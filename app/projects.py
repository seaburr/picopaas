"""Project membership helpers. Apps belong to a project; access is by membership (non-members get 404)."""
from datetime import datetime, timezone
from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def can_see_reason(db: Session, project: models.Project, user_id: Optional[int]) -> bool:
    """The suspension reason is shown to the project's owners only (members just see that it is suspended)."""
    if user_id is None:
        return False
    m = get_membership(db, project.id, user_id)
    return m is not None and m.role == "owner"


def suspended_message(project: models.Project, show_reason: bool = True) -> str:
    reason = (project.suspended_reason or "").strip() if show_reason else ""
    why = f" Reason: {reason}" if reason else ""
    return ("This project is suspended by an administrator, so it is read-only: you can look but not change anything."
            f"{why} Contact an administrator to get it restored.")


def forbid_if_suspended(db: Session, project: Optional[models.Project]) -> None:
    """403 (with the admin's reason) when `project` is suspended and the current request is a write. The request
    method is recorded on the session by get_current_user (every authenticated route depends on it), so every helper
    below enforces this for all members, owners included. Fails closed: a session that never went through
    get_current_user (no method recorded, e.g. the Kaisen worker's) is treated as a write, so a suspended project is
    refused; the worker also has its own earlier stop check (app/kaisen/builder.py)."""
    if project is None or project.status != "suspended":
        return
    method = db.info.get("http_method")
    if method in SAFE_METHODS:
        return
    raise HTTPException(status_code=403, detail=suspended_message(project, can_see_reason(db, project, db.info.get("user_id"))))


def personal_slug(user_id: int) -> str:
    return f"personal-{user_id}"


def create_personal_project(db: Session, user: models.User) -> models.Project:
    """Create the user's personal project + owner membership (flushes; caller commits). User must have an id."""
    project = models.Project(name="Personal", slug=personal_slug(user.id), is_personal=True, created_by=user.id)
    project.members.append(models.ProjectMember(user_id=user.id, role="owner"))
    db.add(project)
    db.flush()
    return project


def get_personal_project(db: Session, user: models.User) -> models.Project:
    project = db.query(models.Project).filter(models.Project.slug == personal_slug(user.id)).first()
    if project is None:  # defensive: users created before the backfill ran
        project = create_personal_project(db, user)
        db.commit()
    return project


def member_project_ids(db: Session, user_id: int):
    return (db.query(models.ProjectMember.project_id)
            .join(models.Project, models.Project.id == models.ProjectMember.project_id)
            .filter(models.ProjectMember.user_id == user_id, models.Project.deleted_at.is_(None)))


def member_app_filter(db: Session, user: models.User):
    """SQL criterion: apps the user may manage as a project member (legacy NULL project_id falls back to creator)."""
    return or_(models.App.project_id.in_(member_project_ids(db, user.id).scalar_subquery()),
               (models.App.project_id.is_(None)) & (models.App.owner_id == user.id))


def get_app_for_member(db: Session, app_id: int, user: models.User, *, allow_admin: bool = False,
                       allow_suspended: bool = False, detail: str = "We can't find that app. It may have been deleted.") -> models.App:
    """The one ownership check: the live app if `user` is a member of its project (or a platform admin when
    allow_admin), else 404 (no existence leak)."""
    q = db.query(models.App).filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    if not (allow_admin and user.is_platform_admin):
        q = q.filter(member_app_filter(db, user))
    app: Optional[models.App] = q.first()
    if app is None:
        raise HTTPException(status_code=404, detail=detail)
    if not allow_suspended and not (allow_admin and user.is_platform_admin) and app.project_id is not None:
        forbid_if_suspended(db, db.get(models.Project, app.project_id))
    return app


# --- roles, limits, lookup helpers (BE-3/BE-4) -------------------------------------------------

def live_project(db: Session, project_id: int) -> Optional[models.Project]:
    p = db.get(models.Project, project_id)
    return p if p is not None and p.deleted_at is None else None


def get_membership(db: Session, project_id: int, user_id: int) -> Optional[models.ProjectMember]:
    return (db.query(models.ProjectMember)
            .join(models.Project, models.Project.id == models.ProjectMember.project_id)
            .filter(models.ProjectMember.project_id == project_id, models.ProjectMember.user_id == user_id,
                    models.Project.deleted_at.is_(None)).first())


def get_project_for_member(db: Session, project_id: int, user: models.User, *, owner: bool = False,
                           allow_suspended: bool = False) -> tuple[models.Project, models.ProjectMember]:
    """The project and the caller's membership. Non-members get 404 (no existence leak); members who lack the owner
    role on an owner-only action get 403. Writes (non-GET) to a suspended project get 403 with the reason, owners
    included; `allow_suspended` is for the few writes that stay open (leaving the project)."""
    member = get_membership(db, project_id, user.id)
    if member is None:
        raise HTTPException(status_code=404, detail="We can't find that project.")
    if owner and member.role != "owner":
        raise HTTPException(status_code=403, detail="Only a project owner can do that.")
    if not allow_suspended:
        forbid_if_suspended(db, member.project)
    return member.project, member


def lock_project(db: Session, project_id: int) -> None:
    """Row-lock the project (SELECT ... FOR UPDATE; no-op on SQLite) until commit/rollback, so concurrent last-owner,
    member-cap and app-limit checks serialize instead of both passing."""
    db.query(models.Project).filter(models.Project.id == project_id).with_for_update().first()


def owner_count(db: Session, project_id: int) -> int:
    return db.query(models.ProjectMember).filter_by(project_id=project_id, role="owner").count()


def live_app_count(db: Session, project_id: int) -> int:
    return db.query(models.App).filter(models.App.project_id == project_id, models.App.deleted_at.is_(None)).count()


def effective_project_app_limit(db: Session, project: models.Project) -> int:
    """Active-app limit of a project. Personal: the owner's own limit (resources.effective_app_limit). Shared: the
    admin-set project.app_limit or the default, never above the per-account ceiling."""
    from app import config, platform_settings, resources
    if project.is_personal:
        return resources.effective_app_limit(db.get(models.User, project.created_by))
    base = project.app_limit if project.app_limit is not None else config.PROJECT_DEFAULT_APP_LIMIT
    return min(base, platform_settings.get(db, "max_apps_per_user"))


def check_app_limit(db: Session, project: models.Project) -> None:
    lock_project(db, project.id)
    eff = effective_project_app_limit(db, project)
    if live_app_count(db, project.id) >= eff:
        plural = "" if eff == 1 else "s"
        if project.is_personal:
            msg = f"You've reached your limit of {eff} app{plural}. Delete one, or ask an administrator to raise your limit."
        else:
            msg = (f"This project has reached its limit of {eff} app{plural}. Delete one, "
                   "or ask an administrator to raise this project's limit.")
        raise HTTPException(status_code=403, detail=msg)


def owner_emails_map(db: Session, project_ids) -> dict[int, list[str]]:
    """project id -> emails of its (active) owners: where operational notices for a project's apps are routed."""
    ids = {i for i in project_ids if i is not None}
    if not ids:
        return {}
    rows = (db.query(models.ProjectMember.project_id, models.User.email)
            .join(models.User, models.User.id == models.ProjectMember.user_id)
            .filter(models.ProjectMember.project_id.in_(ids), models.ProjectMember.role == "owner",
                    models.User.status != "deleted").order_by(models.User.email).all())
    out: dict[int, list[str]] = {}
    for pid, email in rows:
        out.setdefault(pid, []).append(email)
    return out


def owners_label(db: Session, app: models.App, fallback: str = "") -> str:
    emails = owner_emails_map(db, [app.project_id]).get(app.project_id) if app.project_id else None
    return ", ".join(emails) if emails else fallback


# --- suspension (docs/design/suspensions.md) ----------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def suspend_app_row(app: models.App, via: str, by: Optional[models.User], reason: Optional[str]) -> None:
    app.status = "suspended"
    app.status_detail = None
    app.suspended_via = via
    app.suspended_at = _now()
    app.suspended_by = by.id if by is not None else None
    app.suspended_reason = reason


def restore_app_row(app: models.App) -> None:
    app.status = "pending"
    app.status_detail = None
    app.suspended_via = None
    app.suspended_at = None
    app.suspended_by = None
    app.suspended_reason = None
    app.reset_retry()


def live_project_apps(db: Session, project_id: int) -> list:
    return db.query(models.App).filter(models.App.project_id == project_id, models.App.deleted_at.is_(None)).all()


def suspend_project(db: Session, project: models.Project, admin: models.User, reason: str, *,
                    by_user: bool = False) -> int:
    """Suspend `project` and every live app in it that is not already suspended (those keep their own reason).
    `by_user` marks a personal project suspended because its owner was (those apps are tagged via='user').
    Returns how many apps were suspended. Caller commits."""
    project.status = "suspended"
    project.suspended_at = _now()
    project.suspended_by = admin.id
    project.suspended_reason = reason
    project.suspended_by_user = by_user
    n = 0
    for a in live_project_apps(db, project.id):
        if a.status != "suspended":
            suspend_app_row(a, "user" if by_user else "project", admin, reason)
            n += 1
    return n


def unsuspend_project(db: Session, project: models.Project, vias: Iterable[str] = ("project",)) -> int:
    """Lift a project's suspension and restore only the apps that suspension put to sleep (suspended_via in `vias`).
    Apps suspended on their own (via='admin') stay suspended. Returns how many apps were restored. Caller commits."""
    project.status = "active"
    project.suspended_at = None
    project.suspended_by = None
    project.suspended_reason = None
    project.suspended_by_user = False
    vias = tuple(vias)
    n = 0
    for a in live_project_apps(db, project.id):
        if a.status == "suspended" and a.suspended_via in vias:
            restore_app_row(a)
            n += 1
    return n
