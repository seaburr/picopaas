"""Rule-based security alerts for platform admins ("identity velocity": machine-speed attackers such as ARTEX).

`run(db)` reads the audit log, AuthThrottle and KaisenEvent, evaluates fixed rules and upserts `SecurityAlert` rows.
No LLM, no outbound mail. All thresholds live in app/config.py (SECURITY_ALERT_*).

Merging: an open alert is keyed by `dedupe_key` (kind + subject). A run that sees the same condition again merges into
it (count + 1, last_seen moved) only when the condition has NEW evidence (a newer triggering event), so re-reading the
same audit rows every poll does not inflate the count. Acknowledging renames the key (`<key>#ack<id>`), so the next new
evidence opens a fresh alert; evidence not newer than the acknowledged alert's last_seen is ignored.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session

from app import config, models
from app.kaisen.models import KaisenEvent

log = logging.getLogger(__name__)

CLI_ACTORS = ("manage-cli", "root-management-key")
# Rule 1: admin identity/policy actions (the audit action names recorded by app/routers/admin*.py).
ADMIN_ACTIONS = frozenset({
    "user.approve", "user.reject", "user.suspend", "user.unsuspend", "user.promote", "user.demote",
    "user.reset_password", "user.delete", "user.update", "settings.update", "banner.update",
    "kaisen.uploads_flag", "kaisen.web_search_flag", "project.update", "project.suspend", "project.unsuspend",
    "dbserver.create", "dbserver.update", "db_server.delete", "apikey.admin_revoke",
})
MASS_ACCOUNT_ACTIONS = frozenset({"user.approve", "user.delete", "user.reject"})
OUTSIDE_WEB_ACTIONS = frozenset({"user.promote", "user.reset_password"})


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Finding:
    kind: str
    severity: str
    key: str  # dedupe key (kind + subject)
    message: str
    first_seen: datetime
    last_seen: datetime  # time of the newest triggering event
    subject_type: Optional[str] = None
    subject_id: Optional[str] = None
    evidence: dict = field(default_factory=dict)


def _burst(times: list[datetime], need: int, window: timedelta):
    """Sliding window over sorted `times`: (count, first, last) of the largest window holding >= `need` events that ends
    latest, or None. `last` is the newest event of that window."""
    times = sorted(times)
    best = None
    lo = 0
    for hi, t in enumerate(times):
        while t - times[lo] > window:
            lo += 1
        n = hi - lo + 1
        if n >= need:
            best = (n, times[lo], t)
    return best


def _audit(db: Session, actions: Iterable[str], since: datetime) -> list[models.AuditLog]:
    return list(db.scalars(select(models.AuditLog).where(
        models.AuditLog.action.in_(list(actions)), models.AuditLog.at >= since).order_by(models.AuditLog.at)))


def _actor_label(row: models.AuditLog) -> Optional[tuple[str, str, str]]:
    """(dedupe part, subject_type, subject_id) for a user actor or a known CLI actor; None for anything else."""
    if row.actor_id is not None:
        return f"user:{row.actor_id}", "user", str(row.actor_id)
    if row.actor_email in CLI_ACTORS:
        return f"actor:{row.actor_email}", None, None
    return None


def _mins(td: timedelta) -> int:
    return int(td.total_seconds() // 60)


# 1 ------------------------------------------------------------------------------------------------------------
def rule_admin_burst(db: Session, now: datetime) -> list[Finding]:
    window = timedelta(minutes=config.SECURITY_ALERT_ADMIN_BURST_MINUTES)
    rows = _audit(db, ADMIN_ACTIONS, now - timedelta(minutes=config.SECURITY_ALERT_LOOKBACK_MINUTES))
    by: dict = defaultdict(list)
    for r in rows:
        lab = _actor_label(r)
        if lab:
            by[lab].append(r.at)
    out = []
    for (part, st, sid), times in by.items():
        hit = _burst(times, config.SECURITY_ALERT_ADMIN_BURST_ACTIONS, window)
        if hit:
            n, first, last = hit
            ev = {"actions": n, "window_minutes": _mins(window)}
            if st is None:
                ev["actor"] = part.split(":", 1)[1]
            out.append(Finding(
                "admin_burst", "high", f"admin_burst:{part}",
                f"One admin actor made {n} identity or policy changes within {_mins(window)} minutes.",
                first, last, st, sid, ev))
    return out


# 2 ------------------------------------------------------------------------------------------------------------
def rule_outside_web(db: Session, now: datetime) -> list[Finding]:
    since = now - timedelta(minutes=config.SECURITY_ALERT_LOOKBACK_MINUTES)
    out = []
    by: dict = defaultdict(list)
    for r in _audit(db, OUTSIDE_WEB_ACTIONS, since):
        if r.actor_id is None and r.actor_email in CLI_ACTORS:
            by[(r.action, r.target_id, r.actor_email)].append(r)
    for (action, target, actor), rs in by.items():
        out.append(Finding(
            "privilege_outside_web", "medium", f"privilege_outside_web:{action}:{target}",
            f"{action} was run by {actor} (outside the web admin flow) on a user.",
            rs[0].at, rs[-1].at, "user" if target else None, target,
            {"action": action, "actor": actor, "events": len(rs)}))
    window = timedelta(minutes=config.SECURITY_ALERT_ROOT_FAIL_MINUTES)
    fails = [r.at for r in _audit(db, ["root.auth_failed"], since)]
    if fails:
        hit = _burst(fails, config.SECURITY_ALERT_ROOT_FAIL_HIGH, window)
        high = hit is not None
        first, last = (hit[1], hit[2]) if hit else (min(fails), max(fails))
        out.append(Finding(
            "root_auth_failed", "high" if high else "medium", "root_auth_failed",
            f"The root management key was rejected {hit[0] if hit else len(fails)} time(s)"
            + (f" within {_mins(window)} minutes." if high else "."),
            first, max(fails), None, None,
            {"failures": hit[0] if hit else len(fails), "window_minutes": _mins(window)}))
    return out


# 3 ------------------------------------------------------------------------------------------------------------
def rule_new_key_burst(db: Session, now: datetime) -> list[Finding]:
    since = now - timedelta(minutes=config.SECURITY_ALERT_LOOKBACK_MINUTES)
    window = timedelta(minutes=config.SECURITY_ALERT_NEWKEY_MINUTES)
    creators = {a for a in db.scalars(select(models.AuditLog.actor_id).where(
        models.AuditLog.action == "apikey.create", models.AuditLog.at >= since, models.AuditLog.actor_id.is_not(None)))}
    if not creators:
        return []
    # Bounded: only rows by users who created a key in the window (plus the rate-limited markers), never the whole log.
    rows = list(db.scalars(select(models.AuditLog).where(
        models.AuditLog.at >= since,
        (models.AuditLog.actor_id.in_(creators)) | (models.AuditLog.action == "api_key.rate_limited"))
        .order_by(models.AuditLog.at).limit(config.SECURITY_ALERT_MAX_AUDIT_ROWS)))
    by_user: dict = defaultdict(list)
    limited: dict = defaultdict(list)
    for r in rows:
        if r.action == "api_key.rate_limited" and r.target_id is not None:
            limited[str(r.target_id)].append(r.at)  # target is the key owner's user id
        if r.actor_id is not None:
            by_user[r.actor_id].append(r)
    out = []
    for r in rows:
        if r.action != "apikey.create" or r.actor_id is None:
            continue
        end = r.at + window
        acts = [x for x in by_user[r.actor_id] if r.at < x.at <= end and x.id != r.id]
        lim = [t for t in limited.get(str(r.actor_id), []) if r.at <= t <= end]
        if len(acts) >= config.SECURITY_ALERT_NEWKEY_ACTIONS or lim:
            last = max([x.at for x in acts] + lim) if (acts or lim) else r.at
            reason = "rate limited" if lim else f"{len(acts)} audited actions"
            out.append(Finding(
                "new_key_burst", "medium", f"new_key_burst:{r.target_id}",
                f"A new API key was created and its user then showed machine-speed activity ({reason}) "
                f"within {_mins(window)} minutes.",
                r.at, last, "api_key", str(r.target_id),
                {"user_id": r.actor_id, "actions": len(acts), "rate_limited": bool(lim),
                 "window_minutes": _mins(window)}))
    return out


# 4 ------------------------------------------------------------------------------------------------------------
def rule_login_pressure(db: Session, now: datetime) -> list[Finding]:
    window = timedelta(minutes=config.SECURITY_ALERT_LOGIN_MINUTES)
    rows = _audit(db, ["login.failed"], now - timedelta(minutes=config.SECURITY_ALERT_LOOKBACK_MINUTES))
    out = []
    hit = _burst([r.at for r in rows], config.SECURITY_ALERT_LOGIN_FAILS, window)
    if hit:
        out.append(Finding(
            "login_pressure", "medium", "login_pressure",
            f"{hit[0]} failed logins across all accounts within {_mins(window)} minutes.",
            hit[1], hit[2], None, None, {"failures": hit[0], "window_minutes": _mins(window)}))
    per: dict = defaultdict(list)
    for r in rows:
        per[(r.actor_email or "").lower()].append(r.at)
    locked = [_burst(t, config.LOGIN_EMAIL_MAX_FAILS, window) for t in per.values()]
    locked = [h for h in locked if h]
    if len(locked) >= config.SECURITY_ALERT_LOGIN_LOCKED_EMAILS:
        out.append(Finding(
            "login_lockouts", "medium", "login_lockouts",
            f"{len(locked)} different accounts hit the failed-login lockout within {_mins(window)} minutes.",
            min(h[1] for h in locked), max(h[2] for h in locked), None, None,
            {"locked_emails": len(locked), "per_email_fails": config.LOGIN_EMAIL_MAX_FAILS,
             "window_minutes": _mins(window)}))
    return out


# 5 ------------------------------------------------------------------------------------------------------------
def rule_invalid_keys(db: Session, now: datetime) -> list[Finding]:
    since = now - timedelta(minutes=config.SECURITY_ALERT_LOOKBACK_MINUTES)
    rows = db.scalars(select(models.AuthThrottle).where(
        models.AuthThrottle.key.like(config.SECURITY_ALERT_INVALID_KEY_PREFIX + "%"),
        models.AuthThrottle.window_start >= since,
        models.AuthThrottle.count >= config.SECURITY_ALERT_INVALID_KEY_COUNT)).all()
    if not rows:
        return []
    top = max(rows, key=lambda r: r.count)
    return [Finding(
        "invalid_api_keys", "medium", "invalid_api_keys",
        f"{top.count} invalid API keys were presented in one minute.",
        min(r.window_start for r in rows), max(r.window_start for r in rows), None, None,
        {"invalid_keys": top.count, "window_minutes": 1, "minutes_over": len(rows)})]


# 6 ------------------------------------------------------------------------------------------------------------
def rule_kaisen_spend(db: Session, now: datetime) -> list[Finding]:
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    base_days = config.SECURITY_ALERT_KAISEN_BASELINE_DAYS
    since = day0 - timedelta(days=base_days)
    ev = db.scalars(select(KaisenEvent).where(KaisenEvent.at >= since).order_by(KaisenEvent.at)).all()
    by_project: dict = defaultdict(list)
    for e in ev:
        by_project[e.project_id].append(e)
    owners: dict = defaultdict(set)
    if by_project:
        for pid, uid in db.execute(select(models.ProjectMember.project_id, models.ProjectMember.user_id).where(
                models.ProjectMember.role == "owner", models.ProjectMember.project_id.in_(list(by_project)))):
            owners[pid].add(uid)
    out = []
    hour_usd = config.SECURITY_ALERT_KAISEN_HOUR_USD
    for pid, es in by_project.items():
        today = [e for e in es if e.at >= day0]
        prior = sum(e.usd for e in es if e.at < day0)
        avg = prior / base_days
        spent = sum(e.usd for e in today)
        if today and spent >= config.SECURITY_ALERT_KAISEN_SPIKE_MIN_USD and spent >= config.SECURITY_ALERT_KAISEN_SPIKE_FACTOR * avg:
            out.append(Finding(
                "kaisen_spend_spike", "medium", f"kaisen_spend_spike:{pid}",
                f"Kaisen spend today is ${spent:.2f}, at least {config.SECURITY_ALERT_KAISEN_SPIKE_FACTOR:g}x the "
                f"${avg:.2f} daily average of the previous {base_days} days.",
                today[0].at, today[-1].at, "project", str(pid),
                {"today_usd": round(spent, 2), "avg_daily_usd": round(avg, 2), "baseline_days": base_days}))
        buckets: dict = defaultdict(list)
        member_buckets: dict = defaultdict(list)
        for e in es:
            if e.at < now - timedelta(hours=24):
                continue
            h = e.at.replace(minute=0, second=0, microsecond=0)
            buckets[h].append(e)
            if e.user_id is not None and e.user_id not in owners[pid]:
                member_buckets[(h, e.user_id)].append(e)
        hot = [(h, sum(e.usd for e in b), b) for h, b in buckets.items() if sum(e.usd for e in b) >= hour_usd]
        if hot:
            h, usd, b = max(hot, key=lambda x: x[0])
            out.append(Finding(
                "kaisen_hour_spend", "medium", f"kaisen_hour_spend:{pid}",
                f"Kaisen spend reached ${usd:.2f} in a single hour (threshold ${hour_usd:g}).",
                min(e.at for e in b), max(e.at for e in b), "project", str(pid),
                {"hour_usd": round(usd, 2), "threshold_usd": hour_usd}))
        mhot = [(k, sum(e.usd for e in b), b) for k, b in member_buckets.items() if sum(e.usd for e in b) >= hour_usd]
        if mhot:
            (h, uid), usd, b = max(mhot, key=lambda x: x[0][0])
            out.append(Finding(
                "kaisen_member_spend", "medium", f"kaisen_member_spend:{pid}:{uid}",
                f"A project member who is not the owner spent ${usd:.2f} on Kaisen in a single hour "
                f"(threshold ${hour_usd:g}).",
                min(e.at for e in b), max(e.at for e in b), "project", str(pid),
                {"member_user_id": uid, "hour_usd": round(usd, 2), "threshold_usd": hour_usd}))
    return out


# 7 ------------------------------------------------------------------------------------------------------------
def rule_mass_account_changes(db: Session, now: datetime) -> list[Finding]:
    window = timedelta(minutes=config.SECURITY_ALERT_MASS_ACCOUNT_MINUTES)
    rows = _audit(db, MASS_ACCOUNT_ACTIONS, now - window - timedelta(minutes=config.SECURITY_ALERT_LOOKBACK_MINUTES))
    hit = _burst([r.at for r in rows], config.SECURITY_ALERT_MASS_ACCOUNT_CHANGES, window)
    if not hit:
        return []
    return [Finding(
        "mass_account_changes", "high", "mass_account_changes",
        f"{hit[0]} account approvals, rejections or deletions within {_mins(window)} minutes.",
        hit[1], hit[2], None, None, {"changes": hit[0], "window_minutes": _mins(window)})]


RULES = (rule_admin_burst, rule_outside_web, rule_new_key_burst, rule_login_pressure, rule_invalid_keys,
         rule_kaisen_spend, rule_mass_account_changes)
_RANK = {"low": 0, "medium": 1, "high": 2}


def upsert(db: Session, f: Finding) -> Optional[models.SecurityAlert]:
    """Merge into the open alert, or open a new one. Returns the row when something changed."""
    open_row = db.scalar(select(models.SecurityAlert).where(models.SecurityAlert.dedupe_key == f.key))
    if open_row is not None:
        if f.last_seen <= open_row.last_seen:
            return None  # nothing new since the last run
        open_row.count += 1
        open_row.last_seen = f.last_seen
        open_row.message = f.message[:255]
        open_row.evidence = f.evidence
        if _RANK[f.severity] > _RANK[open_row.severity]:
            open_row.severity = f.severity
        return open_row
    acked = db.scalar(select(func.max(models.SecurityAlert.last_seen)).where(
        models.SecurityAlert.dedupe_key.like(f.key + "#ack%")))
    if acked is not None and f.last_seen <= acked:
        return None  # that evidence was already acknowledged
    row = models.SecurityAlert(
        kind=f.kind, severity=f.severity, subject_type=f.subject_type, subject_id=f.subject_id,
        message=f.message[:255], evidence=f.evidence, first_seen=f.first_seen, last_seen=f.last_seen,
        count=1, dedupe_key=f.key)
    db.add(row)
    return row


def purge(db: Session, now: datetime) -> int:
    """Acknowledged alerts past retention go; then the table is capped at SECURITY_ALERT_MAX_ROWS (acknowledged first,
    then the oldest by last_seen)."""
    n = db.execute(delete(models.SecurityAlert).where(
        models.SecurityAlert.acknowledged_at.is_not(None),
        models.SecurityAlert.acknowledged_at < now - timedelta(days=config.SECURITY_ALERT_ACK_RETENTION_DAYS))).rowcount or 0
    total = db.scalar(select(func.count()).select_from(models.SecurityAlert)) or 0
    extra = total - config.SECURITY_ALERT_MAX_ROWS
    if extra > 0:
        # acknowledged first, then lowest severity, then oldest: a flood of low alerts cannot evict an open high one
        sev = case((models.SecurityAlert.severity == "high", 2), (models.SecurityAlert.severity == "medium", 1), else_=0)
        ids = db.scalars(select(models.SecurityAlert.id).order_by(
            models.SecurityAlert.acknowledged_at.is_(None), sev, models.SecurityAlert.last_seen).limit(extra)).all()
        n += db.execute(delete(models.SecurityAlert).where(models.SecurityAlert.id.in_(ids))).rowcount or 0
    return n


def run(db: Session, now: Optional[datetime] = None) -> int:
    """Evaluate every rule once. A failing rule is logged and skipped. Returns the number of alerts opened or merged."""
    now = now or _now()
    changed = 0
    for rule in RULES:
        try:
            for f in rule(db, now):
                if upsert(db, f) is not None:
                    changed += 1
            db.commit()
        except Exception:
            db.rollback()
            log.exception("security alert rule %s failed", rule.__name__)
    try:
        purge(db, now)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("security alert purge failed")
    return changed


def acknowledge(db: Session, alert: models.SecurityAlert, admin: models.User, now: Optional[datetime] = None) -> None:
    alert.acknowledged_at = now or _now()
    alert.acknowledged_by = admin.id
    if "#ack" not in alert.dedupe_key:
        alert.dedupe_key = f"{alert.dedupe_key}#ack{alert.id}"[:160]
    db.commit()


def open_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(models.SecurityAlert).where(
        models.SecurityAlert.acknowledged_at.is_(None))) or 0
