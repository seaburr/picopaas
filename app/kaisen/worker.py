"""Kaisen worker: `python -m app.kaisen.worker`. Runs queued builds one step at a time.

Deployed as its own Deployment (same image, 1 replica) so a long model stream cannot starve the API. It needs DB
access only: no Kubernetes credentials. Leader election is a MySQL GET_LOCK (SQLite = always leader). It is never
imported by app.main, so the API process starts no loops."""
import logging
import os
import signal
import threading
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select, text, update

from app import config, db as appdb, models
from . import builder, deploy as deploy_mod, pushing
from .github import GitHubError, RealGitHub
from .llm import AnthropicClient, ModelError
from .models import KaisenBuild, KaisenEvent, KaisenInbox, KaisenRun
from .paths import ToolError
from .redact import redact
from .settings import load as load_settings, load_for_build, output_for

log = logging.getLogger("app.kaisen.worker")


class Lease:
    """Leader lock. MySQL named locks belong to one connection, so we keep that connection for the process lifetime."""

    def __init__(self, engine=None):
        self.engine = engine or appdb.engine
        self.conn = None

    @property
    def _mysql(self) -> bool:
        return self.engine.dialect.name == "mysql"

    def acquire(self) -> bool:
        if not self._mysql:
            return True
        try:
            if self.conn is None:
                self.conn = self.engine.connect()
            got = self.conn.execute(text("SELECT GET_LOCK(:n, 0)"), {"n": config.KAISEN_WORKER_LOCK_NAME}).scalar()
            self.conn.commit()
            return got == 1
        except Exception:
            log.exception("kaisen worker lock check failed")
            self._drop()
            return False

    def check(self) -> bool:
        """Still the leader? Re-checked before every model call, push and deploy."""
        if not self._mysql:
            return True
        try:
            if self.conn is None:
                return False
            ok = self.conn.execute(text("SELECT IS_USED_LOCK(:n) = CONNECTION_ID()"),
                                   {"n": config.KAISEN_WORKER_LOCK_NAME}).scalar()
            self.conn.commit()
            return bool(ok)
        except Exception:
            self._drop()
            return False

    def _drop(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


def touch_heartbeat(path: str | None = None) -> None:
    """The pod's liveness probe reads this file's mtime."""
    p = Path(path or config.KAISEN_HEARTBEAT_FILE)
    try:
        p.touch()
    except OSError:
        log.warning("could not touch the heartbeat file %s", p)


def session_factory():
    return appdb.SessionLocal()


def model_factory(api_key: str):
    return AnthropicClient(api_key=api_key)


def github_factory(token: str):
    return RealGitHub(token)


def make_deployer(S, registry_factory, runtime, wait_s, clock=time.monotonic, sleep=time.sleep):
    """The builder's deploy_preview tool: (session, run) -> (ok, text). Raises ToolError."""
    def _deployer(s, run):
        s.commit()  # persist this turn's file writes: a deploy failure rolls the session back
        b = s.get(KaisenBuild, run.build_id)
        rs = load_for_build(s, b)
        try:
            app, _ = deploy_mod.deploy_build(s, b, registry_factory(), force=False)
        except deploy_mod.DeployError as e:
            msg = redact(str(e), rs.secret_values())
            s.add(KaisenInbox(build_id=b.id, run_id=run.id, kind="deploy", text=f"Preview not deployed: {msg}"))
            s.commit()
            suffix = " Code changes cannot fix this; call finish and tell the human." if e.final else ""
            raise ToolError(msg + suffix) from None
        s.commit()  # do not hold a transaction open while waiting
        res = deploy_mod.check(runtime, app.id, rs.secret_values(), wait_s, clock, sleep)
        return res.ok, res.text
    return _deployer


def deploy_on_finish(S, run_id, registry_factory, runtime, wait_s, sleep=time.sleep) -> None:
    with S() as s:
        run = s.get(KaisenRun, run_id)
        b = s.get(KaisenBuild, run.build_id)
        rs = load_for_build(s, b)
        try:
            app, changed = deploy_mod.deploy_build(s, b, registry_factory(), force=False)
            res = deploy_mod.check(runtime, app.id, rs.secret_values(), wait_s, sleep=sleep)
            text_ = ("Preview deployed: " if res.ok else "Preview deployed but not healthy yet: ") + res.text
        except deploy_mod.DeployError as e:
            text_ = f"Automatic preview deploy failed: {e}"
        s.add(KaisenInbox(build_id=b.id, run_id=run_id, kind="deploy", text=redact(text_, rs.secret_values())))
        s.commit()


def push_on_finish(S, run_id, gh_factory) -> None:
    with S() as s:
        run = s.get(KaisenRun, run_id)
        b = s.get(KaisenBuild, run.build_id)
        rs = load_for_build(s, b)
        out = output_for(b, rs)
        if not (out.push_enabled and out.push_on_finish):
            return
        try:
            msg = pushing.push_build(s, b, out, gh_factory)
        except GitHubError as e:
            msg = redact(f"Automatic push failed: {e}. Fix the build's Output settings, then press Push on the build page.",
                         rs.secret_values())
        s.add(KaisenInbox(build_id=b.id, run_id=run_id, kind="push", text=msg))
        s.commit()


def tick(S, lease, *, model_fn=model_factory, gh_fn=github_factory, registry_factory=None, runtime=None,
         wait_s: float = config.KAISEN_PREVIEW_WAIT_TOOL, clock=time.monotonic, sleep=time.sleep) -> bool:
    """Run one queued step if we hold the lease. Returns True if work was attempted."""
    if not lease.acquire():
        return False
    with S() as s:
        run_id = s.scalar(select(KaisenRun.id).where(KaisenRun.status.in_(("queued", "running")))
                          .order_by(KaisenRun.id).limit(1))
        if run_id is None:
            return False
        run = s.get(KaisenRun, run_id)
        b = s.get(KaisenBuild, run.build_id)
        if b.deleted_at is not None:
            builder.set_status(s, run, "stopped")
            s.commit()
            return True
        rs = load_settings(s, b.project_id)
    if registry_factory is None:
        from app.dependencies import get_registry
        registry_factory = get_registry
    runtime = runtime or deploy_mod.PlatformRuntime(S)
    if not rs.anthropic_api_key:
        builder.stop_run(S, run_id, "error", "error", "No Anthropic API key is set for this project. Add one in Kaisen settings.")
        return True
    try:
        client = model_fn(rs.anthropic_api_key)
    except ModelError as e:
        builder.stop_run(S, run_id, "error", "error", str(e))
        return True
    deployer = make_deployer(S, registry_factory, runtime, wait_s, clock, sleep)
    if builder.run_step(S, run_id, client, lease, deployer=deployer) == "done":
        if lease.check():
            deploy_on_finish(S, run_id, registry_factory, runtime, wait_s, sleep)
        if lease.check():
            push_on_finish(S, run_id, gh_fn)
    return True


def retention(S, now=None) -> dict:
    """Trim old transcripts (14 days) and metering events (90 days). Bundles and files go with their build."""
    now = now or builder._now()
    with S() as s:
        t = s.execute(update(KaisenRun).where(
            KaisenRun.finished_at.is_not(None), KaisenRun.finished_at < now - timedelta(days=config.KAISEN_TRANSCRIPT_DAYS),
            KaisenRun.status.in_(("done", "stopped", "error"))).values(messages=[], pending=None)).rowcount
        e = s.execute(delete(KaisenEvent).where(
            KaisenEvent.at < now - timedelta(days=config.KAISEN_EVENT_DAYS))).rowcount
        s.commit()
    return {"transcripts": t, "events": e}


class Worker:
    def __init__(self, S=session_factory, lease=None, **tick_kwargs):
        self.S, self.lease, self.tick_kwargs = S, lease or Lease(), tick_kwargs
        self.stop = threading.Event()
        self._last_retention = 0.0

    def heartbeat_loop(self):
        while not self.stop.is_set():
            touch_heartbeat()
            self.stop.wait(config.KAISEN_HEARTBEAT_SECONDS)

    def run_once(self) -> bool:
        touch_heartbeat()
        try:
            busy = tick(self.S, self.lease, **self.tick_kwargs)
            if time.monotonic() - self._last_retention > 3600:
                self._last_retention = time.monotonic()
                retention(self.S)
        except Exception:
            log.exception("kaisen worker tick failed")
            busy = False
        return busy

    def run(self):
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()  # alive even during a long model stream
        while not self.stop.is_set():
            busy = self.run_once()
            self.stop.wait(config.KAISEN_WORKER_BUSY_SECONDS if busy else config.KAISEN_WORKER_IDLE_SECONDS)


def main() -> None:
    from app import logging_setup
    logging_setup.configure()
    w = Worker()
    signal.signal(signal.SIGTERM, lambda *_: w.stop.set())
    log.info("kaisen worker started")
    touch_heartbeat()
    w.run()


if __name__ == "__main__":
    main()
