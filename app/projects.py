"""Project membership helpers. Apps belong to a project; access is by membership (non-members get 404)."""
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models


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
                       detail: str = "We can't find that app. It may have been deleted.") -> models.App:
    """The one ownership check: the live app if `user` is a member of its project (or a platform admin when
    allow_admin), else 404 (no existence leak)."""
    q = db.query(models.App).filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    if not (allow_admin and user.is_platform_admin):
        q = q.filter(member_app_filter(db, user))
    app: Optional[models.App] = q.first()
    if app is None:
        raise HTTPException(status_code=404, detail=detail)
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


def get_project_for_member(db: Session, project_id: int, user: models.User, *, owner: bool = False
                           ) -> tuple[models.Project, models.ProjectMember]:
    """The project and the caller's membership. Non-members get 404 (no existence leak); members who lack the owner
    role on an owner-only action get 403."""
    member = get_membership(db, project_id, user.id)
    if member is None:
        raise HTTPException(status_code=404, detail="We can't find that project.")
    if owner and member.role != "owner":
        raise HTTPException(status_code=403, detail="Only a project owner can do that.")
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
