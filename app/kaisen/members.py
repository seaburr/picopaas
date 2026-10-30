"""Per-member spend limits (owners set them; enforced next to the project caps in builder.check_caps).

A run counts against the member who started it (KaisenRun.started_by, copied onto each KaisenEvent.user_id). Events
written before this existed have no user and count only against the project caps."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config, models
from .models import KaisenEvent, KaisenMemberLimit


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _day_start() -> datetime:
    return _now().replace(hour=0, minute=0, second=0, microsecond=0)


def spend_today(s: Session, project_id: int, user_id: int) -> float:
    return s.scalar(select(func.coalesce(func.sum(KaisenEvent.usd), 0.0)).where(
        KaisenEvent.project_id == project_id, KaisenEvent.user_id == user_id, KaisenEvent.at >= _day_start()))


def spend_on_build(s: Session, build_id: int, user_id: int) -> float:
    return s.scalar(select(func.coalesce(func.sum(KaisenEvent.usd), 0.0)).where(
        KaisenEvent.build_id == build_id, KaisenEvent.user_id == user_id))


def limits(s: Session, project_id: int, user_id: int) -> tuple[Optional[float], Optional[float]]:
    row = s.get(KaisenMemberLimit, (project_id, user_id))
    return (row.cap_day, row.cap_build) if row is not None else (None, None)


def check(s: Session, project_id: int, build_id: int, user_id: Optional[int], *, mine: bool = True) -> str | None:
    """A plain-language reason this member may not spend more, or None."""
    if user_id is None:
        return None
    cap_day, cap_build = limits(s, project_id, user_id)
    who = "Your" if mine else "The starter's"
    spent_by = "you spent" if mine else "they spent"
    if cap_day is not None and (spent := spend_today(s, project_id, user_id)) >= cap_day:
        return (f"{who} member daily limit of ${cap_day:.2f} in this project is reached ({spent_by} ${spent:.2f} today). "
                "A project owner can raise it in Kaisen settings; it resets at 00:00 UTC.")
    if cap_build is not None and (spent := spend_on_build(s, build_id, user_id)) >= cap_build:
        return (f"{who} member per-build limit of ${cap_build:.2f} is reached on this build ({spent_by} ${spent:.2f}). "
                "A project owner can raise it in Kaisen settings.")
    return None


def describe(s: Session, project_id: int, viewer_id: int, is_owner: bool) -> list[dict]:
    """Owners see every member; a member sees only their own row."""
    q = (select(models.ProjectMember, models.User).join(models.User, models.User.id == models.ProjectMember.user_id)
         .where(models.ProjectMember.project_id == project_id).order_by(models.User.email))
    out = []
    for m, u in s.execute(q):
        if not is_owner and u.id != viewer_id:
            continue
        cap_day, cap_build = limits(s, project_id, u.id)
        out.append({"user_id": u.id, "email": u.email, "role": m.role, "cap_day": cap_day, "cap_build": cap_build,
                    "spend_today_usd": round(spend_today(s, project_id, u.id), 6)})
    return out


def save(s: Session, project_id: int, user_id: int, by: int, data: dict) -> None:
    """`data` holds only the fields the client sent; null clears a limit. Raises ValueError (plain message). No commit."""
    row = s.get(KaisenMemberLimit, (project_id, user_id))
    new = {"cap_day": row.cap_day if row else None, "cap_build": row.cap_build if row else None}
    for k in ("cap_day", "cap_build"):
        if k not in data:
            continue
        v = data[k]
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= config.KAISEN_CAP_MAX):
            raise ValueError(f"{k} must be empty (no limit) or a number between 0 and {config.KAISEN_CAP_MAX:g} (US dollars).")
        new[k] = None if v is None else float(v)
    if row is None:
        row = KaisenMemberLimit(project_id=project_id, user_id=user_id)
        s.add(row)
    row.cap_day, row.cap_build, row.updated_by, row.updated_at = new["cap_day"], new["cap_build"], by, _now()
