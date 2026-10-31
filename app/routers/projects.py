"""Projects: team ownership of apps. CRUD, members, invites. Non-members always get 404; owner-only actions 403.

Membership changes (invites, roles, removal, leave, accept) need a browser session: API keys act as the user for app
work but cannot manage who is in a project."""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import audit, config, models, projects, ratelimit
from app.db import get_db
from app.dependencies import get_active_user, require_session_user

router = APIRouter(tags=["projects"])
Role = Literal["owner", "member"]
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Give the project a name.")
        return v


class ProjectOut(BaseModel):
    id: int
    name: str
    slug: str
    is_personal: bool
    role: Role  # the caller's role
    member_count: int
    app_count: int
    app_limit: Optional[int]  # admin-set override (null = default)
    app_limit_effective: int
    created_at: datetime
    status: str = "active"  # active | suspended (read-only, show the banner)
    suspended_reason: Optional[str] = None
    suspended_at: Optional[datetime] = None


class MemberOut(BaseModel):
    user_id: int
    email: str
    role: Role
    added_at: datetime


class RolePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role


class InviteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: Optional[str] = Field(default=None, max_length=255)  # omit for a link anyone with an account can use
    role: Role = "member"

    @field_validator("email")
    @classmethod
    def _email(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("That doesn't look like an email address.")
        return v

    @model_validator(mode="after")
    def _link_is_member_only(self):
        if self.email is None and self.role != "member":
            raise ValueError("An invite link can only add a member. To invite an owner, enter their email.")
        return self


class InviteOut(BaseModel):
    id: int
    email: Optional[str]
    role: Role
    created_at: datetime
    expires_at: datetime


class InviteCreatedOut(InviteOut):
    token: str  # shown once; only its hash is stored
    path: str   # /invite/<token>: the UI builds the shareable link from this


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _project_out(db: Session, project: models.Project, role: str) -> ProjectOut:
    return ProjectOut(
        id=project.id, name=project.name, slug=project.slug, is_personal=project.is_personal, role=role,
        member_count=db.query(models.ProjectMember).filter_by(project_id=project.id).count(),
        app_count=projects.live_app_count(db, project.id), app_limit=project.app_limit,
        app_limit_effective=projects.effective_project_app_limit(db, project), created_at=project.created_at,
        status=project.status, suspended_at=project.suspended_at,
        suspended_reason=project.suspended_reason if role == "owner" else None)  # the reason is for owners only


def _slug_for(db: Session, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "project"
    while True:
        slug = f"{base}-{secrets.token_hex(2)}"
        if db.query(models.Project.id).filter(models.Project.slug == slug).first() is None:
            return slug


def _ensure_unique_name(db: Session, user: models.User, name: str, exclude_id: Optional[int] = None) -> None:
    """Project names are unique per user (case-insensitive, trimmed) among the projects they belong to.
    Only the caller's own projects are consulted, so other users' names are never revealed."""
    q = (db.query(models.Project.id)
         .join(models.ProjectMember, models.ProjectMember.project_id == models.Project.id)
         .filter(models.ProjectMember.user_id == user.id, models.Project.deleted_at.is_(None),
                 func.lower(func.trim(models.Project.name)) == name.strip().lower()))
    if exclude_id is not None:
        q = q.filter(models.Project.id != exclude_id)
    if q.first() is not None:
        raise HTTPException(status_code=409, detail=f"You already have a project named {name}.")


def _shared_only(project: models.Project) -> None:
    if project.is_personal:
        raise HTTPException(status_code=409, detail="Your personal project can't be shared, renamed or deleted. Create a new project for a team.")


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    owned = (db.query(models.ProjectMember)
             .join(models.Project, models.Project.id == models.ProjectMember.project_id)
             .filter(models.ProjectMember.user_id == user.id, models.ProjectMember.role == "owner",
                     models.Project.is_personal.is_(False), models.Project.deleted_at.is_(None)).count())
    if owned >= config.MAX_PROJECTS_PER_USER:
        raise HTTPException(status_code=403, detail=f"You can own up to {config.MAX_PROJECTS_PER_USER} shared projects. Delete one first.")
    _ensure_unique_name(db, user, payload.name)
    project = models.Project(name=payload.name, slug=_slug_for(db, payload.name), is_personal=False, created_by=user.id)
    project.members.append(models.ProjectMember(user_id=user.id, role="owner"))
    db.add(project)
    db.commit()
    audit.record(db, user, "project.create", "project", project.id, f"project={project.id} name={project.name}")
    return _project_out(db, project, "owner")


@router.get("/projects", response_model=List[ProjectOut])
def list_projects(db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    rows = (db.query(models.ProjectMember)
            .join(models.Project, models.Project.id == models.ProjectMember.project_id)
            .filter(models.ProjectMember.user_id == user.id, models.Project.deleted_at.is_(None))
            .order_by(models.Project.is_personal.desc(), models.Project.name).all())
    return [_project_out(db, m.project, m.role) for m in rows]


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    project, member = projects.get_project_for_member(db, project_id, user)
    return _project_out(db, project, member.role)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def rename_project(project_id: int, payload: ProjectCreate, db: Session = Depends(get_db),
                   user: models.User = Depends(get_active_user)):
    project, member = projects.get_project_for_member(db, project_id, user, owner=True)
    _shared_only(project)
    _ensure_unique_name(db, user, payload.name, exclude_id=project.id)
    project.name = payload.name
    db.commit()
    audit.record(db, user, "project.update", "project", project.id, f"project={project.id} name={project.name}")
    return _project_out(db, project, member.role)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    project, _ = projects.get_project_for_member(db, project_id, user, owner=True)
    _shared_only(project)
    if projects.live_app_count(db, project.id):
        raise HTTPException(status_code=409, detail="This project still has apps. Delete them or move them to another project first.")
    project.deleted_at = _now()
    db.query(models.ProjectInvite).filter(models.ProjectInvite.project_id == project.id,
                                          models.ProjectInvite.accepted_at.is_(None),
                                          models.ProjectInvite.revoked_at.is_(None)).update({"revoked_at": _now()})
    db.commit()
    audit.record(db, user, "project.delete", "project", project.id, f"project={project.id}")
    return None


# --- members ----------------------------------------------------------------------------------

@router.get("/projects/{project_id}/members", response_model=List[MemberOut])
def list_members(project_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    projects.get_project_for_member(db, project_id, user)
    rows = (db.query(models.ProjectMember, models.User.email)
            .join(models.User, models.User.id == models.ProjectMember.user_id)
            .filter(models.ProjectMember.project_id == project_id, models.User.status != "deleted")
            .order_by(models.ProjectMember.added_at, models.ProjectMember.id).all())
    return [MemberOut(user_id=m.user_id, email=email, role=m.role, added_at=m.added_at) for m, email in rows]


def _target_member(db: Session, project_id: int, user_id: int) -> models.ProjectMember:
    m = db.query(models.ProjectMember).filter_by(project_id=project_id, user_id=user_id).first()
    if m is None:
        raise HTTPException(status_code=404, detail="We can't find that member.")
    return m


def _guard_last_owner(db: Session, member: models.ProjectMember) -> None:
    projects.lock_project(db, member.project_id)
    if member.role == "owner" and projects.owner_count(db, member.project_id) <= 1:
        raise HTTPException(status_code=409, detail="A project needs at least one owner. Make someone else an owner first.")


@router.patch("/projects/{project_id}/members/{user_id}", response_model=MemberOut)
def change_role(project_id: int, user_id: int, payload: RolePatch, db: Session = Depends(get_db),
                user: models.User = Depends(require_session_user)):
    project, _ = projects.get_project_for_member(db, project_id, user, owner=True)
    _shared_only(project)
    target = _target_member(db, project_id, user_id)
    if target.role != payload.role:
        if payload.role == "member":
            _guard_last_owner(db, target)
        old = target.role
        target.role = payload.role
        db.commit()
        audit.record(db, user, "project.role_change", "project", project_id,
                     f"project={project_id} user={user_id} {old}->{payload.role}")
    email = db.get(models.User, user_id).email
    return MemberOut(user_id=target.user_id, email=email, role=target.role, added_at=target.added_at)


def _remove(db: Session, actor: models.User, project_id: int, target: models.ProjectMember, *, leaving: bool) -> None:
    _guard_last_owner(db, target)
    uid = target.user_id
    db.delete(target)
    db.commit()
    audit.record(db, actor, "project.remove", "project", project_id,
                 f"project={project_id} user={uid}{' left' if leaving else ''}")


@router.delete("/projects/{project_id}/members/{user_id}", status_code=204)
def remove_member(project_id: int, user_id: int, db: Session = Depends(get_db),
                  user: models.User = Depends(require_session_user)):
    """An owner removes anyone; any member may remove themselves (leave)."""
    project, _ = projects.get_project_for_member(db, project_id, user, owner=user_id != user.id,
                                                 allow_suspended=user_id == user.id)  # leaving stays open
    _shared_only(project)
    _remove(db, user, project_id, _target_member(db, project_id, user_id), leaving=user_id == user.id)
    return None


@router.post("/projects/{project_id}/leave", status_code=204)
def leave_project(project_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_session_user)):
    project, member = projects.get_project_for_member(db, project_id, user, allow_suspended=True)
    _shared_only(project)
    _remove(db, user, project_id, member, leaving=True)
    return None


# --- invites ----------------------------------------------------------------------------------

@router.post("/projects/{project_id}/invites", response_model=InviteCreatedOut, status_code=201)
def create_invite(project_id: int, payload: InviteCreate, db: Session = Depends(get_db),
                  user: models.User = Depends(require_session_user)):
    project, _ = projects.get_project_for_member(db, project_id, user, owner=True)
    _shared_only(project)
    now = _now()
    pending = (db.query(models.ProjectInvite).filter(
        models.ProjectInvite.project_id == project_id, models.ProjectInvite.accepted_at.is_(None),
        models.ProjectInvite.revoked_at.is_(None), models.ProjectInvite.expires_at > now).count())
    if pending >= config.MAX_PENDING_INVITES_PER_PROJECT:
        raise HTTPException(status_code=403, detail="This project has too many open invites. Revoke some first.")
    if payload.email:
        existing = (db.query(models.ProjectMember).join(models.User, models.User.id == models.ProjectMember.user_id)
                    .filter(models.ProjectMember.project_id == project_id, models.User.email == payload.email).first())
        if existing is not None:
            raise HTTPException(status_code=409, detail="That person is already in this project.")
    token = secrets.token_urlsafe(32)
    inv = models.ProjectInvite(project_id=project_id, email=payload.email, token_hash=hash_token(token), role=payload.role,
                               invited_by=user.id, expires_at=now + timedelta(days=config.PROJECT_INVITE_TTL_DAYS))
    db.add(inv)
    db.commit()
    audit.record(db, user, "project.invite", "project", project_id,
                 f"project={project_id} invite={inv.id} role={inv.role}{' email-bound' if inv.email else ' link'}")
    return InviteCreatedOut(id=inv.id, email=inv.email, role=inv.role, created_at=inv.created_at,
                            expires_at=inv.expires_at, token=token, path=f"/invite/{token}")


@router.get("/projects/{project_id}/invites", response_model=List[InviteOut])
def list_invites(project_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    projects.get_project_for_member(db, project_id, user, owner=True)
    return (db.query(models.ProjectInvite).filter(
        models.ProjectInvite.project_id == project_id, models.ProjectInvite.accepted_at.is_(None),
        models.ProjectInvite.revoked_at.is_(None), models.ProjectInvite.expires_at > _now())
        .order_by(models.ProjectInvite.id).all())


@router.delete("/projects/{project_id}/invites/{invite_id}", status_code=204)
def revoke_invite(project_id: int, invite_id: int, db: Session = Depends(get_db),
                  user: models.User = Depends(require_session_user)):
    projects.get_project_for_member(db, project_id, user, owner=True)
    inv = db.query(models.ProjectInvite).filter_by(id=invite_id, project_id=project_id).first()
    if inv is None or inv.accepted_at is not None or inv.revoked_at is not None:
        raise HTTPException(status_code=404, detail="We can't find that invite.")
    inv.revoked_at = _now()
    db.commit()
    audit.record(db, user, "project.invite_revoke", "project", project_id, f"project={project_id} invite={invite_id}")
    return None


_DEAD = "This invite has expired or was already used. Ask a project owner for a new one."


@router.post("/invites/{token}/accept", response_model=ProjectOut)
def accept_invite(token: str, request: Request, db: Session = Depends(get_db),
                  user: models.User = Depends(require_session_user)):
    """Join the project. Needs a login (pending/suspended accounts are refused by the auth dependency). Counted against
    a per-user and per-IP rate limit on every attempt."""
    ratelimit.check_invite_accept(db, request, user.id)
    inv = db.query(models.ProjectInvite).filter_by(token_hash=hash_token(token)).first()
    project = projects.live_project(db, inv.project_id) if inv is not None else None
    if inv is None or project is None:
        raise HTTPException(status_code=404, detail="We can't find that invite. Check the link.")
    if inv.accepted_at is not None or inv.revoked_at is not None or inv.expires_at <= _now():
        raise HTTPException(status_code=410, detail=_DEAD)
    if inv.email and inv.email != user.email.lower():
        raise HTTPException(status_code=403, detail="This invite was sent to a different email address. Log in with that account.")
    projects.forbid_if_suspended(db, project)
    projects.lock_project(db, project.id)
    if projects.get_membership(db, project.id, user.id) is not None:
        raise HTTPException(status_code=409, detail="You're already in this project.")
    if db.query(models.ProjectMember).filter_by(project_id=project.id).count() >= config.MAX_PROJECT_MEMBERS:
        raise HTTPException(status_code=403, detail="This project is full. Ask an owner to remove someone first.")
    inv.accepted_at = _now()
    db.add(models.ProjectMember(project_id=project.id, user_id=user.id, role=inv.role))
    db.commit()
    audit.record(db, user, "project.join", "project", project.id, f"project={project.id} role={inv.role} invite={inv.id}")
    return _project_out(db, project, inv.role)
