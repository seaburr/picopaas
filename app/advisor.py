"""Rule-based health advisor (phase 1; design in docs/research/health-advisor-2026-11-11.md).

A rule is a pure function `(AppFacts) -> Finding | None`: no I/O, so every rule is a table-driven unit test. `load_facts`
fills AppFacts from rows PicoPaaS already has (batched queries, no cluster calls). The advisor never changes anything: an
action only describes a call the frontend may make to an EXISTING endpoint after a confirm.

Every threshold lives in app/config.py (ADVISOR_*); ceilings and floors come from app/resources.py.
At a ceiling (platform cap, or a quota only an admin can raise) a finding has no action and its text never points at an
admin: it states the fact and what the owner can do.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session, selectinload

from app import config, image_check, image_history, models, resources
from app.reconciler import backoff_for

_MIB = 1024 * 1024
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
# Query options for callers that evaluate many apps at once (admin list): avoids one lazy load per app.
LOAD_OPTIONS = (selectinload(models.App.database), selectinload(models.App.storage), selectinload(models.App.image_history))


# --- data ---------------------------------------------------------------------------------------

@dataclass
class EventFact:
    kind: str  # oom | crashed | disk | evicted | ...
    at: datetime  # naive UTC
    message: str = ""


@dataclass
class SampleFact:
    """One metrics sample reduced to per-pod maxima (limits apply per pod; the stored cpu/mem are sums over pods)."""
    ts: datetime
    cpu_m: int
    mem_mi: int
    pods: frozenset = frozenset()


@dataclass
class BackupFact:
    enabled: bool = True
    schedule: str = "daily"
    admin_disabled: bool = False
    created_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    failed: list = field(default_factory=list)  # [(label, error | None)] for every kind whose last run failed


@dataclass
class AppFacts:
    app_id: int
    name: str
    now: datetime  # naive UTC
    created_at: datetime
    status: str = "running"
    status_reason: Optional[str] = None
    status_detail: Optional[str] = None
    error_age_s: Optional[int] = None  # lower bound on how long the app has been in error
    port: int = 8080
    image: str = "example/demo:1.0"
    image_digest: Optional[str] = None
    auto_update: bool = True
    rejected_digest: Optional[str] = None
    image_update_error: Optional[str] = None
    health_check_enabled: bool = False
    health_check_path: str = "/healthz"
    # What runs now / the most the platform allows / the least an owner may pick. Keys: cpu_limit_m, mem_limit_mi,
    # ephemeral_limit_mi, storage_quota_mb.
    limits: dict = field(default_factory=lambda: {"cpu_limit_m": 200, "mem_limit_mi": 384, "ephemeral_limit_mi": 250,
                                                  "storage_quota_mb": 100})
    ceilings: dict = field(default_factory=lambda: {"cpu_limit_m": 200, "mem_limit_mi": 384, "ephemeral_limit_mi": 250,
                                                    "storage_quota_mb": 100})
    floors: dict = field(default_factory=lambda: {"cpu_limit_m": 50, "mem_limit_mi": 64, "ephemeral_limit_mi": 64,
                                                  "storage_quota_mb": 1})
    # Add-ons (None = not attached)
    db_size_bytes: Optional[int] = None
    db_quota_bytes: int = 0
    db_over_quota: bool = False
    db_created_at: Optional[datetime] = None
    bucket_size_bytes: Optional[int] = None
    bucket_quota_bytes: int = 0
    bucket_over_quota: bool = False
    bucket_created_at: Optional[datetime] = None
    volume_size_gi: Optional[int] = None
    volume_used_bytes: Optional[int] = None
    events: list = field(default_factory=list)  # EventFact, any order
    samples: list = field(default_factory=list)  # SampleFact, oldest first, already settled (see `settle`)
    current_digest_at: Optional[datetime] = None  # when the running digest was recorded as a good version
    previous_digest: Optional[str] = None  # newest other good version (rollback target)
    backup: Optional[BackupFact] = None


@dataclass
class Finding:
    rule: str
    severity: str
    title: str
    evidence: str
    action: Optional[dict] = None

    def as_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity, "title": self.title, "evidence": self.evidence,
                "action": self.action}


# --- small helpers ------------------------------------------------------------------------------

def _dur(seconds: float) -> str:
    seconds = int(seconds)
    if seconds % 86400 == 0 and seconds > 86400:
        return f"{seconds // 86400} d"
    if seconds % 3600 == 0 and seconds >= 3600:
        return f"{seconds // 3600} h"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{max(1, round(seconds / 60))} min"


def _ago(now: datetime, then: datetime) -> str:
    return _dur(max(0, (now - then).total_seconds()))


def _clock(dt: datetime) -> str:
    return dt.strftime("%H:%M UTC")


def _mb(n_bytes: float) -> str:
    return f"{n_bytes / _MIB:.1f}"


def _round_up(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


METRICS_WINDOW_S = config.METRICS_RETENTION_POINTS * config.METRICS_SAMPLE_SECONDS
_UNIT = {"cpu_limit_m": "m", "mem_limit_mi": "MB", "ephemeral_limit_mi": "MB", "storage_quota_mb": "MB"}
_NOUN = {"cpu_limit_m": "CPU", "mem_limit_mi": "memory", "ephemeral_limit_mi": "scratch disk",
         "storage_quota_mb": "bucket quota"}
_STEP = {"cpu_limit_m": config.ADVISOR_CPU_ROUND_M, "mem_limit_mi": config.ADVISOR_MEM_ROUND_MI,
         "ephemeral_limit_mi": config.ADVISOR_MEM_ROUND_MI, "storage_quota_mb": 1}


def link(label: str, to: str) -> dict:
    return {"kind": "link", "label": label, "to": to}


def _raise_target(f: AppFacts, field_: str) -> Optional[int]:
    """The value a raise would set: current x factor, rounded up to the step, clamped to [floor, ceiling]. None at the ceiling."""
    cur, ceil = f.limits[field_], f.ceilings[field_]
    if cur >= ceil:
        return None
    target = max(f.floors[field_], min(ceil, _round_up(cur * config.ADVISOR_RAISE_FACTOR, _STEP[field_])))
    return target if target > cur else None


def _raise_action(f: AppFacts, field_: str, restarts: bool = True) -> Optional[dict]:
    """A set_resource action when the owner can raise `field_` himself within the ceiling; None at the ceiling.
    The frontend sends PATCH /apps/{id} with {"resources": {field: to}}."""
    to = _raise_target(f, field_)
    if to is None:
        return None
    unit = _UNIT[field_]
    return {"kind": "set_resource", "label": f"Raise {_NOUN[field_]} to {to} {unit}", "field": field_,
            "from": f.limits[field_], "to": to, "unit": unit, "restarts_app": restarts}


def _cap_text(f: AppFacts, field_: str, what: str) -> str:
    return f"At the platform cap ({f.ceilings[field_]} {_UNIT[field_]}). Reduce {what}."


def _events(f: AppFacts, kind: str, window_s: int) -> list[EventFact]:
    since = f.now - timedelta(seconds=window_s)
    return sorted((e for e in f.events if e.kind == kind and e.at >= since), key=lambda e: e.at)


_CODE = re.compile(r"exited with code (\d+)")


def _exit_code(ev: EventFact) -> Optional[str]:
    m = _CODE.search(ev.message or "")
    return m.group(1) if m else None


def _longest_run(flags: Iterable[bool]) -> int:
    best = cur = 0
    for x in flags:
        cur = cur + 1 if x else 0
        best = max(best, cur)
    return best


def settle(samples: list[SampleFact]) -> list[SampleFact]:
    """Drop samples taken within ADVISOR_POD_SETTLE_S of a pod first appearing (start-up spikes). Pods already present in
    the oldest sample started earlier than we can tell, so they never trigger the guard."""
    if not samples:
        return []
    first_pods = samples[0].pods
    first_seen: dict[str, datetime] = {}
    for s in samples:
        for p in s.pods:
            first_seen.setdefault(p, s.ts)
    limit = timedelta(seconds=config.ADVISOR_POD_SETTLE_S)
    return [s for s in samples
            if not any(p not in first_pods and s.ts - first_seen[p] < limit for p in s.pods)]


# --- rules --------------------------------------------------------------------------------------

def rule_mem_oom(f: AppFacts) -> Optional[Finding]:
    ev = _events(f, "oom", config.ADVISOR_OOM_WINDOW_S)
    if not ev:
        return None
    sev = "critical" if len(ev) >= config.ADVISOR_OOM_CRITICAL_COUNT else "warning"
    limit = f.limits["mem_limit_mi"]
    text = f"OOM-killed {len(ev)} time{'s' if len(ev) != 1 else ''} in {_dur(config.ADVISOR_OOM_WINDOW_S)} (last {_clock(ev[-1].at)}). Memory limit {limit} MB"
    if f.samples:
        text += f", peak {max(s.mem_mi for s in f.samples)} MB in the last {_dur(METRICS_WINDOW_S)}"
    action = _raise_action(f, "mem_limit_mi")
    text += ". " + (_cap_text(f, "mem_limit_mi", "memory use") if action is None else "")
    return Finding("mem.oom", sev, "Out of memory", text.strip(), action)


def rule_mem_near_limit(f: AppFacts) -> Optional[Finding]:
    limit = f.limits["mem_limit_mi"]
    hot = [s.mem_mi * 100 >= config.ADVISOR_USAGE_PCT * limit for s in f.samples]
    run = _longest_run(hot)
    if run < config.ADVISOR_USAGE_MIN_SAMPLES:
        return None
    peak = max(s.mem_mi for s in f.samples)
    text = (f"Memory at or above {config.ADVISOR_USAGE_PCT}% of its {limit} MB limit for {run} samples in a row "
            f"(peak {peak} MB, last {_dur(METRICS_WINDOW_S)}). The app is likely to be OOM-killed. ")
    action = _raise_action(f, "mem_limit_mi")
    if action is None:
        text += _cap_text(f, "mem_limit_mi", "memory use")
    return Finding("mem.near_limit", "warning", "Memory near its limit", text.strip(), action)


def rule_cpu_saturated(f: AppFacts) -> Optional[Finding]:
    limit = f.limits["cpu_limit_m"]
    hot = [s.cpu_m * 100 >= config.ADVISOR_USAGE_PCT * limit for s in f.samples]
    n, n_hot, run = len(f.samples), sum(hot), _longest_run(hot)
    if n < config.ADVISOR_USAGE_MIN_SAMPLES:
        return None
    if run < config.ADVISOR_USAGE_MIN_SAMPLES and n_hot * 100 < config.ADVISOR_CPU_SHARE_PCT * n:
        return None
    text = (f"CPU at or above {config.ADVISOR_USAGE_PCT}% of its {limit} m limit in {n_hot} of {n} samples "
            f"(last {_dur(METRICS_WINDOW_S)}), longest run {run} samples. Pinned at its CPU limit. ")
    action = _raise_action(f, "cpu_limit_m")
    if action is None:
        text += _cap_text(f, "cpu_limit_m", "CPU use")
    return Finding("cpu.saturated", "warning", "CPU pinned at its limit", text.strip(), action)


def _loop_burst(f: AppFacts) -> list[EventFact]:
    """Latest run of LOOP_CRASHES crashes inside LOOP_WINDOW_S that ended recently, else []."""
    ev = _events(f, "crashed", config.ADVISOR_FLAP_WINDOW_S)
    n, span = config.ADVISOR_LOOP_CRASHES, timedelta(seconds=config.ADVISOR_LOOP_WINDOW_S)
    recent = f.now - timedelta(seconds=config.ADVISOR_LOOP_RECENT_S)
    for i in range(len(ev) - n, -1, -1):
        if ev[i + n - 1].at - ev[i].at <= span and ev[i + n - 1].at >= recent:
            return ev[i:i + n]
    return []


def rule_restart_loop(f: AppFacts) -> Optional[Finding]:
    burst = _loop_burst(f)
    by_status = f.status_reason == "crash_loop"
    if not burst and not by_status:
        return None
    ev = _events(f, "crashed", config.ADVISOR_FLAP_WINDOW_S)
    if ev:
        code = _exit_code(ev[-1])
        text = (f"{len(ev)} crash{'es' if len(ev) != 1 else ''} in {_dur(config.ADVISOR_FLAP_WINDOW_S)} "
                f"(last {_clock(ev[-1].at)}{', exit code ' + code if code else ''}); status is "
                f"{'crash_loop' if by_status else 'restarting repeatedly'}.")
    else:
        text = "Status is crash_loop: the container starts and exits repeatedly."
    action: dict = link("Open logs", f"/apps/{f.app_id}#logs")
    if f.current_digest_at is not None and f.previous_digest and f.image_digest:
        after = [e for e in ev if e.at >= f.current_digest_at]
        if after and (after[0].at - f.current_digest_at).total_seconds() <= config.ADVISOR_ROLLBACK_CORRELATION_S:
            prev = image_check.short_digest(f.previous_digest)
            text += (f" Started after the image update {prev} -> {image_check.short_digest(f.image_digest)} "
                     f"({_ago(f.now, f.current_digest_at)} ago).")
            action = {"kind": "rollback", "label": f"Roll back to {prev}", "digest": f.previous_digest,
                      "restarts_app": True}
    return Finding("restart.loop", "critical", "Restart loop", text, action)


def rule_restart_flapping(f: AppFacts) -> Optional[Finding]:
    ev = _events(f, "crashed", config.ADVISOR_FLAP_WINDOW_S)
    if len(ev) < config.ADVISOR_FLAP_CRASHES:
        return None
    code = _exit_code(ev[-1])
    text = (f"Exited {len(ev)} times in {_dur(config.ADVISOR_FLAP_WINDOW_S)} (last {_clock(ev[-1].at)}"
            f"{', exit code ' + code if code else ''}).")
    return Finding("restart.flapping", "warning", "App keeps exiting", text, link("Open logs", f"/apps/{f.app_id}#logs"))


def rule_disk_scratch(f: AppFacts) -> Optional[Finding]:
    ev = _events(f, "disk", config.ADVISOR_DISK_WINDOW_S)
    if not ev:
        return None
    text = (f"Scratch disk limit ({f.limits['ephemeral_limit_mi']} MB) exceeded {len(ev)} time{'s' if len(ev) != 1 else ''} in "
            f"{_dur(config.ADVISOR_DISK_WINDOW_S)} (last {_clock(ev[-1].at)}); the app was evicted. "
            "Scratch files are lost on restart; write persistent files to the bucket or a volume.")
    action = _raise_action(f, "ephemeral_limit_mi")
    if action is None:
        text += " " + _cap_text(f, "ephemeral_limit_mi", "scratch file use")
    return Finding("disk.scratch", "warning", "Scratch disk full", text, action)


def _quota_severity(size: int, quota: int, over: bool, warn: int, crit: int) -> Optional[str]:
    if quota <= 0:
        return None
    if over or size * 100 >= crit * quota:
        return "critical"
    if size * 100 >= warn * quota:
        return "warning"
    return None


def rule_db_near_quota(f: AppFacts) -> Optional[Finding]:
    if f.db_size_bytes is None:
        return None
    sev = _quota_severity(f.db_size_bytes, f.db_quota_bytes, f.db_over_quota,
                          config.ADVISOR_QUOTA_WARN_PCT, config.ADVISOR_QUOTA_CRIT_PCT)
    if sev is None:
        return None
    pct = round(f.db_size_bytes * 100 / f.db_quota_bytes)
    head = "Database over its quota" if f.db_over_quota else "Database near quota"
    text = f"Database holds {_mb(f.db_size_bytes)} of {f.db_quota_bytes // _MIB} MB ({pct}%). Delete data to get under the quota."
    return Finding("db.near_quota", sev, head, text, None)


def rule_bucket_near_quota(f: AppFacts) -> Optional[Finding]:
    if f.bucket_size_bytes is None:
        return None
    sev = _quota_severity(f.bucket_size_bytes, f.bucket_quota_bytes, f.bucket_over_quota,
                          config.ADVISOR_QUOTA_WARN_PCT, config.ADVISOR_QUOTA_CRIT_PCT)
    if sev is None:
        return None
    pct = round(f.bucket_size_bytes * 100 / f.bucket_quota_bytes)
    head = "Bucket over its quota" if f.bucket_over_quota else "Bucket near quota"
    text = f"Bucket holds {_mb(f.bucket_size_bytes)} of {f.bucket_quota_bytes // _MIB} MB ({pct}%)."
    action = _raise_action(f, "storage_quota_mb", restarts=False)
    if action is None:
        text += " Delete objects to get under the quota."
    return Finding("bucket.near_quota", sev, head, text, action)


def rule_volume_near_full(f: AppFacts) -> Optional[Finding]:
    if not f.volume_size_gi or f.volume_used_bytes is None:
        return None
    size = f.volume_size_gi * 1024 * _MIB
    sev = _quota_severity(f.volume_used_bytes, size, False, config.ADVISOR_VOLUME_WARN_PCT, config.ADVISOR_VOLUME_CRIT_PCT)
    if sev is None:
        return None
    pct = round(f.volume_used_bytes * 100 / size)
    text = f"Volume holds {_mb(f.volume_used_bytes)} of {f.volume_size_gi * 1024} MB ({pct}%). Delete files from the volume."
    return Finding("volume.near_full", sev, "Volume near full", text, None)


def rule_addon_unused(f: AppFacts) -> Optional[Finding]:
    """Database or bucket attached for a while and still empty. (Volumes are left out: no attach date is stored.)"""
    cutoff = f.now - timedelta(days=config.ADVISOR_ADDON_UNUSED_DAYS)
    unused = []
    if f.db_size_bytes == 0 and f.db_created_at is not None and f.db_created_at <= cutoff:
        unused.append(("Database", f.db_created_at))
    if f.bucket_size_bytes == 0 and f.bucket_created_at is not None and f.bucket_created_at <= cutoff:
        unused.append(("Bucket", f.bucket_created_at))
    if not unused:
        return None
    text = " ".join(f"{name} attached {_ago(f.now, at)} ago, still empty (0 MB)." for name, at in unused)
    return Finding("addon.unused", "info", " and ".join(n for n, _ in unused) + " unused", text, None)


def rule_image_mutable_tag(f: AppFacts) -> Optional[Finding]:
    if image_check.is_digest_ref(f.image) or image_check.image_tag(f.image) != "latest" or not f.auto_update:
        return None
    text = (f"Image {f.image} uses the tag latest, which moves. A push redeploys the app within "
            f"{config.IMAGE_WATCH_MINUTES} min. A version tag controls when updates ship.")
    return Finding("image.mutable_tag", "info", "Mutable image tag", text, link("Open settings", f"/apps/{f.app_id}/edit"))


def rule_image_rejected(f: AppFacts) -> Optional[Finding]:
    if not (f.rejected_digest or f.image_update_error):
        return None
    bits = []
    if f.rejected_digest:
        bits.append(f"Image {image_check.short_digest(f.rejected_digest)} failed to start; the previous version is running.")
    if f.image_update_error:
        bits.append(f"Last image update: {f.image_update_error}")
    return Finding("image.rejected", "warning", "Newest image rejected", " ".join(bits),
                   link("Open versions", f"/apps/{f.app_id}#h-versions"))


def rule_health_none(f: AppFacts) -> Optional[Finding]:
    if f.health_check_enabled or f.status != "running":
        return None
    if (f.now - f.created_at).total_seconds() < config.ADVISOR_HEALTH_NONE_MIN_AGE_S:
        return None
    text = (f"Only a TCP check on port {f.port} runs. A hung process that still accepts connections is not restarted. "
            "An HTTP health check restarts it.")
    return Finding("health.none", "info", "No HTTP health check", text, link("Open settings", f"/apps/{f.app_id}/edit"))


def rule_health_failing(f: AppFacts) -> Optional[Finding]:
    if f.status != "error" or f.status_reason not in ("not_ready", "timeout"):
        return None
    what = (f"Health check on {f.health_check_path} is not returning 2xx/3xx" if f.health_check_enabled
            else f"TCP check on port {f.port} is not succeeding")
    text = f"Status error ({f.status_reason}): {what}."
    return Finding("health.failing", "critical", "Health check failing", text, link("Open logs", f"/apps/{f.app_id}#logs"))


def rule_backup_failing(f: AppFacts) -> Optional[Finding]:
    b = f.backup
    if b is None or not b.enabled or b.admin_disabled or b.schedule not in config.ADVISOR_BACKUP_INTERVAL_DAYS:
        return None
    last = f"Last good backup {_ago(f.now, b.last_success_at)} ago." if b.last_success_at else "No good backup yet."
    if b.failed:
        label, err = b.failed[0]
        text = f"{label} backup failed{': ' + err if err else ''}. {last}"
        return Finding("backup.failing", "warning", "Backup failing", text, link("Open backups", f"/apps/{f.app_id}#h-backups"))
    stale = timedelta(days=config.ADVISOR_BACKUP_INTERVAL_DAYS[b.schedule] * config.ADVISOR_BACKUP_STALE_FACTOR)
    ref = b.last_success_at or b.created_at
    if ref is not None and f.now - ref > stale:
        text = f"{last} The {b.schedule} schedule expects one every {config.ADVISOR_BACKUP_INTERVAL_DAYS[b.schedule]} d."
        return Finding("backup.failing", "warning", "Backups overdue", text, link("Open backups", f"/apps/{f.app_id}#h-backups"))
    return None


def rule_app_stuck_error(f: AppFacts) -> Optional[Finding]:
    if f.status != "error" or f.error_age_s is None or f.error_age_s < config.ADVISOR_STUCK_ERROR_DAYS * 86400:
        return None
    text = f"In error for at least {_dur(f.error_age_s // 86400 * 86400)} ({f.status_reason or 'unknown'})."
    return Finding("app.stuck_error", "info", "Stuck in error", text, None)


# (id, function, admin_only)
RULES: list[tuple[str, Callable[[AppFacts], Optional[Finding]], bool]] = [
    ("mem.oom", rule_mem_oom, False), ("mem.near_limit", rule_mem_near_limit, False),
    ("cpu.saturated", rule_cpu_saturated, False), ("restart.loop", rule_restart_loop, False),
    ("restart.flapping", rule_restart_flapping, False), ("disk.scratch", rule_disk_scratch, False),
    ("db.near_quota", rule_db_near_quota, False), ("bucket.near_quota", rule_bucket_near_quota, False),
    ("volume.near_full", rule_volume_near_full, False), ("addon.unused", rule_addon_unused, False),
    ("image.mutable_tag", rule_image_mutable_tag, False), ("image.rejected", rule_image_rejected, False),
    ("health.none", rule_health_none, False), ("health.failing", rule_health_failing, False),
    ("backup.failing", rule_backup_failing, False), ("app.stuck_error", rule_app_stuck_error, True),
]
# One open finding per resource: the key suppresses the findings listed.
_SUPERSEDES = {"mem.oom": ("mem.near_limit",), "restart.loop": ("restart.flapping",)}


def evaluate(f: AppFacts, *, admin: bool = False) -> list[Finding]:
    """Findings for one app, critical first. Guard rails: nothing for suspended apps or apps younger than
    ADVISOR_MIN_APP_AGE_S. Admin-only rules are included only with admin=True."""
    if f.status == "suspended" or (f.now - f.created_at).total_seconds() < config.ADVISOR_MIN_APP_AGE_S:
        return []
    out = [x for rid, fn, admin_only in RULES if (admin or not admin_only) and (x := fn(f)) is not None]
    drop = {d for x in out for d in _SUPERSEDES.get(x.rule, ())}
    out = [x for x in out if x.rule not in drop]
    return sorted(out, key=lambda x: SEVERITY_ORDER[x.severity])


# --- loading ------------------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sample_fact(row: models.AppMetricSample) -> SampleFact:
    import json
    try:
        data = json.loads(row.per_pod) if row.per_pod else {}
        pods = {str(k): (int(v[0]), int(v[1])) for k, v in data.items()}
    except Exception:  # noqa: BLE001 - a malformed historical row must never break advice
        pods = {}
    if pods:
        return SampleFact(row.ts, max(c for c, _ in pods.values()), max(m for _, m in pods.values()), frozenset(pods))
    n = max(1, row.pods or 1)  # before per-pod tracking: the mean is the best per-pod figure there is
    return SampleFact(row.ts, -(-row.cpu_m // n), -(-row.mem_mi // n))


def _error_age_s(app: models.App, now: datetime) -> Optional[int]:
    """Lower bound on the time in error: the retry backoffs already waited out plus the time since the last attempt.
    Not retryable = no retries, so only the time since the failure."""
    if app.status != "error" or app.last_attempt_at is None:
        return None
    waited = sum(backoff_for(i).total_seconds() for i in range(1, max(1, app.error_count or 1))) if app.error_retryable else 0
    return int(waited + max(0, (now - app.last_attempt_at).total_seconds()))


def load_facts(db: Session, apps: list[models.App], now: Optional[datetime] = None) -> dict[int, AppFacts]:
    """AppFacts for many apps with one query each for events, samples and backup configs (no per-app queries beyond
    what `apps` already loaded: pass them with LOAD_OPTIONS)."""
    now = now or _utcnow()
    ids = [a.id for a in apps]
    if not ids:
        return {}
    ev_by: dict[int, list[EventFact]] = {i: [] for i in ids}
    since_ev = now - timedelta(seconds=max(config.ADVISOR_DISK_WINDOW_S, config.ADVISOR_OOM_WINDOW_S, config.ADVISOR_FLAP_WINDOW_S))
    for e in db.query(models.AppEvent).filter(models.AppEvent.app_id.in_(ids), models.AppEvent.at >= since_ev).all():
        ev_by[e.app_id].append(EventFact(e.kind, e.at, e.message))
    sm_by: dict[int, list[SampleFact]] = {i: [] for i in ids}
    for r in (db.query(models.AppMetricSample)
              .filter(models.AppMetricSample.app_id.in_(ids), models.AppMetricSample.ts >= now - timedelta(seconds=METRICS_WINDOW_S))
              .order_by(models.AppMetricSample.ts, models.AppMetricSample.id).all()):
        sm_by[r.app_id].append(_sample_fact(r))
    bk_by: dict[int, BackupFact] = {}
    for c in db.query(models.AppBackupConfig).filter(models.AppBackupConfig.app_id.in_(ids)).all():
        failed = [(lbl, err) for lbl, st, err in (("Files", c.last_status, c.last_error), ("Database", c.db_last_status, c.db_last_error),
                                                    ("Volume", c.vol_last_status, c.vol_last_error),
                                                    ("Bucket", c.bucket_last_status, c.bucket_last_error)) if st == "failed"]
        bk_by[c.app_id] = BackupFact(c.enabled, c.schedule, c.admin_disabled, c.created_at, c.last_success_at, failed)
    out: dict[int, AppFacts] = {}
    for a in apps:
        eff, cei = resources.effective(a, db), resources.ceilings(a, db)
        keys = ("cpu_limit_m", "mem_limit_mi", "ephemeral_limit_mi", "storage_quota_mb")
        cur = next((h for h in a.image_history if h.digest == a.image_digest), None) if a.image_digest else None
        prev = image_history.previous_good(a)
        d, s = a.database, a.storage
        out[a.id] = AppFacts(
            app_id=a.id, name=a.name, now=now, created_at=a.created_at, status=a.status, status_reason=a.status_reason,
            status_detail=a.status_detail, error_age_s=_error_age_s(a, now), port=a.port, image=a.image,
            image_digest=a.image_digest, auto_update=a.auto_update, rejected_digest=a.rejected_digest,
            image_update_error=None if a.image_update_error_dismissed else a.image_update_error,
            health_check_enabled=a.health_check_enabled, health_check_path=a.health_check_path,
            limits={k: eff[k] for k in keys}, ceilings={k: cei[k] for k in keys},
            floors=resources.floors(cei, a),
            db_size_bytes=d.size_bytes if d else None, db_quota_bytes=resources.db_quota_bytes(a, db),
            db_over_quota=bool(d and d.over_quota), db_created_at=d.created_at if d else None,
            bucket_size_bytes=s.size_bytes if s else None, bucket_quota_bytes=resources.storage_quota_bytes(a, db),
            bucket_over_quota=bool(s and s.over_quota), bucket_created_at=s.created_at if s else None,
            volume_size_gi=a.volume_size_gi, volume_used_bytes=a.volume_used_bytes,
            events=ev_by[a.id], samples=settle(sm_by[a.id]),
            current_digest_at=cur.created_at if cur else None, previous_digest=prev.digest if prev else None,
            backup=bk_by.get(a.id),
        )
    return out
