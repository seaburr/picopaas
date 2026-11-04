"""GitHub push of a build's files, shared by the Push endpoint (an async job) and push-on-finish."""
import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config

from . import paths, uploads
from .github import GitHubError, PushResult
from .models import KaisenBuild, KaisenFile, KaisenInbox, KaisenPush
from .redact import redact
from .settings import Output, load_for_build, output_for

log = logging.getLogger("picopaas.kaisen.push")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def push_files(db: Session, b: KaisenBuild, out: Output, github_factory) -> PushResult | None:
    """Push to the repo in the build's output settings; None if there is nothing to push. Raises github.GitHubError."""
    rows = [(r.path, r.content) for r in db.scalars(select(KaisenFile).where(KaisenFile.build_id == b.id))]
    skipped = sorted(p for p, _ in rows if paths.is_secret_file(p))  # K91: legacy .env / key files never leave the platform
    files: list[tuple[str, str | bytes]] = [(p, c) for p, c in rows if not paths.is_secret_file(p)]
    if not files:
        return None
    files += uploads.asset_blobs(db, b.id)  # uploaded images go as they are; references never leave the platform
    res = github_factory(out.token).push(
        out.repo, files, b.slug, out.branch or None, f"Kaisen: {b.name}", out.open_pr)
    res.skipped = skipped
    return res


def result_message(res: PushResult | None) -> str:
    if res is None:
        return "Nothing to push yet."
    return (f"Pushed to {res.url}" + (f" (PR: {res.pr_url})" if res.pr_url else "")
            + (" (empty repo was seeded)" if res.seeded else "")
            + (f". Not pushed because they look like secrets: {', '.join(res.skipped)}" if res.skipped else ""))


def push_build(db: Session, b: KaisenBuild, out: Output, github_factory) -> str:
    """Synchronous push returning a message (push-on-finish, which already runs in the worker)."""
    return result_message(push_files(db, b, out, github_factory))


# ---------------------------------------------------------------- async push jobs (the Push button)

def _expire_stale(db: Session, build_id: int) -> None:
    cutoff = _now() - timedelta(seconds=config.KAISEN_PUSH_STALE_S)
    for j in db.scalars(select(KaisenPush).where(KaisenPush.build_id == build_id, KaisenPush.status == "running",
                                                 KaisenPush.created_at < cutoff)):
        j.status, j.error_code, j.finished_at = "error", "interrupted", _now()
        j.message = "The push was interrupted (the platform restarted). Press Push again."
    db.flush()


def latest(db: Session, build_id: int) -> KaisenPush | None:
    _expire_stale(db, build_id)
    return db.scalars(select(KaisenPush).where(KaisenPush.build_id == build_id).order_by(KaisenPush.id.desc()).limit(1)).first()


def view(j: KaisenPush | None) -> dict | None:
    if j is None:
        return None
    return {"id": j.id, "build_id": j.build_id, "status": j.status, "message": j.message, "error_code": j.error_code,
            "url": j.url, "pr_url": j.pr_url, "created_at": j.created_at.isoformat(),
            "finished_at": j.finished_at.isoformat() if j.finished_at else None}


def start(db: Session, b: KaisenBuild, user_id: int) -> tuple[KaisenPush, bool]:
    """Create the job row, or return the one already running for this build. (job, created)"""
    from app import projects
    projects.lock_project(db, b.project_id)  # serialize two simultaneous POSTs (no-op on SQLite): one job per build
    cur = latest(db, b.id)
    if cur is not None and cur.status == "running":
        db.commit()
        return cur, False
    j = KaisenPush(build_id=b.id, started_by=user_id, status="running")
    db.add(j)
    db.commit()
    return j, True


def execute(db: Session, push_id: int, github_factory) -> None:
    """Do the push and record the outcome. Never raises."""
    j = db.get(KaisenPush, push_id)
    b = db.get(KaisenBuild, j.build_id)
    rs = load_for_build(db, b)
    secrets = rs.secret_values()
    try:
        res = push_files(db, b, output_for(b, rs), github_factory)
        j.status, j.message = "done", redact(result_message(res), secrets)
        if res is not None:
            j.url, j.pr_url = res.url, res.pr_url
    except GitHubError as exc:
        db.rollback()
        j = db.get(KaisenPush, push_id)
        j.status, j.message, j.error_code = "error", redact(str(exc), secrets), exc.code
    except Exception:  # noqa: BLE001 - the job row must always reach a final state
        log.exception("kaisen push %s failed unexpectedly", push_id)
        db.rollback()
        j = db.get(KaisenPush, push_id)
        j.status, j.error_code = "error", "internal"
        j.message = "Pushing failed unexpectedly. Try again; if it keeps failing, ask a platform admin to check the logs."
    j.finished_at = _now()
    db.add(KaisenInbox(build_id=j.build_id, kind="push", text=j.message, status="closed"))
    db.commit()


def threaded_runner(job) -> None:
    """Default runner: run `job(db)` on a daemon thread with its own session."""
    from app import db as appdb

    def _go():
        s = appdb.SessionLocal()
        try:
            job(s)
        finally:
            s.close()
    threading.Thread(target=_go, name="kaisen-push", daemon=True).start()
