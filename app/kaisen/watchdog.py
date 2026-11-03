"""Hang watchdog for Kaisen runs.

A step is "hung" when its run has shown no sign of life (`last_progress_at`) for KAISEN_HANG_SECONDS. Detection never
depends on the thread that hung: the worker's waiter, its heartbeat thread and the API pod's reconciler all call
`sweep`. A hang interrupts the run (bumps `attempt`, which fences the abandoned step: it checks the counter before it
persists anything) and requeues it to resume from its persisted messages and files. After KAISEN_HANG_RETRIES
automatic retries the run stops with error_reason "hung". Everything here is compare-and-swap on (id, attempt), so
two detectors cannot both count one hang.

Also keeps the in-process "loop alive" stamp the worker's heartbeat thread reads: the liveness file is only touched
while the main loop is making progress, so a truly wedged worker is restarted by its probe."""
import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from app import config
from .models import KaisenInbox, KaisenRun

log = logging.getLogger("app.kaisen.watchdog")

_lock = threading.Lock()
_alive = time.monotonic()
_phase = ("idle", time.monotonic())


def abandoned() -> bool:
    """True on a thread whose step was abandoned (see worker.guarded_step, builder.bounded_create): it must not
    refresh the liveness stamp or the phase of the retry that replaced it."""
    return bool(getattr(threading.current_thread(), "abandoned", False))


def bump() -> None:
    """The worker's main loop (or the step it runs) is alive."""
    global _alive
    if abandoned():
        return
    with _lock:
        _alive = time.monotonic()


def loop_age(now: float | None = None) -> float:
    return (now if now is not None else time.monotonic()) - _alive


def loop_healthy(now: float | None = None) -> bool:
    return loop_age(now) < config.KAISEN_HANG_SECONDS + config.KAISEN_HANG_MARGIN_SECONDS


def set_phase(name: str) -> None:
    """"model", "tool" or "idle": what the in-flight step is waiting on (for the in-process tool bound)."""
    global _phase, _alive
    if abandoned():
        return
    with _lock:
        _phase = (name, time.monotonic())
        _alive = _phase[1]


def phase_age() -> tuple[str, float]:
    with _lock:
        return _phase[0], time.monotonic() - _phase[1]


def _utc(now: datetime | None) -> datetime:
    from .builder import _now
    return now or _now()


def stamp(run: KaisenRun, now: datetime | None = None) -> None:
    """Record progress on the run (persisted by the caller's next commit) and tell the loop it is alive."""
    run.last_progress_at = _utc(now)
    bump()


def interrupt(S, run_id: int, attempt: int, why: str, *, manual: bool = False, now: datetime | None = None) -> str:
    """Interrupt the in-flight step of `run_id` if it is still attempt `attempt` and running. Returns "requeued",
    "error" (retries exhausted), or "skipped" (somebody else already handled it, or the run moved on)."""
    from .builder import _stop, set_status
    with S() as s:
        n = s.execute(update(KaisenRun).where(
            KaisenRun.id == run_id, KaisenRun.attempt == attempt, KaisenRun.status == "running"
        ).values(attempt=attempt + 1)).rowcount
        if not n:
            s.rollback()
            return "skipped"
        run = s.get(KaisenRun, run_id)
        s.refresh(run)
        limit = config.KAISEN_HANG_RETRIES
        if not manual:
            run.hang_retries = (run.hang_retries or 0) + 1
        if not manual and run.hang_retries > limit:
            _stop(s, run, "error", "error", f"Hung {run.hang_retries} times; Resume to retry.")
            run.error_reason = "hung"
            s.commit()
            log.warning("kaisen run %s hung %s times: stopped", run_id, run.hang_retries)
            return "error"
        text = (f"{why}; interrupted, retrying ({run.hang_retries}/{limit})." if not manual
                else f"{why}; retrying.")
        run.last_progress_at = None
        run.error = None
        set_status(s, run, "queued")
        s.add(KaisenInbox(build_id=run.build_id, run_id=run.id, kind="hang", text=text, status="closed"))
        s.commit()
        log.warning("kaisen run %s interrupted: %s", run_id, text)
        return "requeued"


def stale_runs(s, now: datetime | None = None) -> list[tuple[int, int, float]]:
    """(run id, attempt, idle seconds) of running runs with no progress for KAISEN_HANG_SECONDS."""
    now = _utc(now)
    cutoff = now - timedelta(seconds=config.KAISEN_HANG_SECONDS)
    seen = func.coalesce(KaisenRun.last_progress_at, KaisenRun.created_at)
    rows = s.execute(select(KaisenRun.id, KaisenRun.attempt, seen).where(
        KaisenRun.status == "running", seen < cutoff)).all()
    return [(i, a, (now - t).total_seconds()) for i, a, t in rows]


def sweep(S, now: datetime | None = None) -> list[tuple[int, str]]:
    """Interrupt every stale running run. Safe to call from any thread or process, as often as you like."""
    out = []
    with S() as s:
        stale = stale_runs(s, now)
        s.rollback()
    for run_id, attempt, idle in stale:
        mins = max(1, round(idle / 60))
        out.append((run_id, interrupt(S, run_id, attempt, f"No progress for {mins} min", now=now)))
    return out


MANUAL_INTERRUPTS_PER_HOUR = 3  # per build: an abandoned model call is billed but never metered, so this is a spend bound


def manual_interrupts_recent(s, build_id: int, now: datetime | None = None) -> int:
    since = _utc(now) - timedelta(hours=1)
    return s.scalar(select(func.count(KaisenInbox.id)).where(
        KaisenInbox.build_id == build_id, KaisenInbox.kind == "hang", KaisenInbox.text.like("Interrupted by %"),
        KaisenInbox.created_at >= since)) or 0


def manual_interrupt(S, run_id: int, who: str) -> str:
    """The owner pressed Interrupt: abandon the in-flight step and retry. Not counted against the hang retries."""
    with S() as s:
        run = s.get(KaisenRun, run_id)
        attempt = run.attempt if run else 0
        s.rollback()
    return interrupt(S, run_id, attempt, f"Interrupted by {who}", manual=True)



class _Borrow:
    """A session factory over an existing session that is not closed on exit (the reconciler's)."""

    def __init__(self, s):
        self.s = s

    def __call__(self):
        return self

    def __enter__(self):
        return self.s

    def __exit__(self, *exc):
        return False


def sweep_session(session, now: datetime | None = None) -> list[tuple[int, str]]:
    """`sweep` for callers that own a session (the API pod's reconciler). The worker may be the thing that is wedged,
    so the API pods back its own detection up."""
    return sweep(_Borrow(session), now)


def borrow(session):
    return _Borrow(session)
