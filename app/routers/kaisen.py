"""Kaisen (Preview): per-project settings and builds. Non-members get 404; settings edits are owner-only.

Everything 404s when Kaisen is off (KAISEN_ENABLED and the admin setting kaisen_enabled). Deploys go through the
platform's own spec path as the build's creator (app/kaisen/deploy.py): no PicoPaaS API key is involved."""
import hmac
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, config, models, platform_settings, projects, ratelimit
from app.db import get_db
from app.dependencies import get_active_user, get_registry
from app.kaisen import builder, buildsecrets, deploy as deploy_mod, members as kmembers, export, pushing, reviewing, settings as ksettings, snapshots, testresults
from app.kaisen.github import GitHubError, RealGitHub
from app.kaisen.llm import AnthropicClient, ModelError
from app.kaisen.models import (KaisenBuild, KaisenBundle, KaisenFile, KaisenInbox, KaisenRun, KaisenSecret, KaisenSnapshot)
from app.kaisen.redact import redact, transcript_lines

NOT_FOUND = "That page doesn't exist."


def require_kaisen(db: Session = Depends(get_db)) -> None:
    if not platform_settings.kaisen_on(db):
        raise HTTPException(status_code=404, detail=NOT_FOUND)


# Overridden in tests with fakes (the real ones talk to Anthropic / GitHub).
def get_github_factory():
    return lambda token: RealGitHub(token)


def get_model_factory():
    return lambda api_key: AnthropicClient(api_key=api_key)


router = APIRouter(tags=["kaisen"], dependencies=[Depends(require_kaisen)])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- settings

class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anthropic_key: Optional[str] = Field(default=None, max_length=500)
    clear_anthropic_key: bool = False
    model: Optional[str] = Field(default=None, max_length=64)
    effort: Optional[str] = Field(default=None, max_length=16)
    cap_run: Optional[float] = None
    cap_day: Optional[float] = None
    cap_build: Optional[float] = None
    github_token: Optional[str] = Field(default=None, max_length=500)
    clear_github_token: bool = False
    web_access: Optional[bool] = None
    reviewer: Optional[str] = Field(default=None, max_length=8)
    reviewer_model: Optional[str] = Field(default=None, max_length=64)
    web_allowed_domains: Optional[list[str]] = Field(default=None, max_length=config.KAISEN_WEB_DOMAINS_MAX)


@router.get("/projects/{project_id}/kaisen")
def get_settings(project_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    _, member = projects.get_project_for_member(db, project_id, user)
    return ksettings.describe(db, project_id, can_edit=member.role == "owner")


@router.put("/projects/{project_id}/kaisen")
def put_settings(project_id: int, payload: SettingsIn, db: Session = Depends(get_db),
                 user: models.User = Depends(get_active_user)):
    projects.get_project_for_member(db, project_id, user, owner=True)
    data = payload.model_dump(exclude_unset=True)
    before = ksettings.load(db, project_id)
    try:
        ksettings.save(db, project_id, user.id, data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    audit.record(db, user, "kaisen.settings_update", "project", project_id,
                 "fields=" + ",".join(ksettings.changed_fields(data)))
    after = ksettings.load(db, project_id)
    if (before.web_access, before.web_allowed_domains) != (after.web_access, after.web_allowed_domains):
        audit.record(db, user, "kaisen.web_access_update", "project", project_id,
                     f"web_access={'on' if after.web_access else 'off'} domains={len(after.web_allowed_domains)}")
    return ksettings.describe(db, project_id, can_edit=True)


class MemberLimitsIn(BaseModel):
    """null clears a limit (no limit). Omitted fields are kept."""
    model_config = ConfigDict(extra="forbid")
    cap_day: Optional[float] = None
    cap_build: Optional[float] = None


@router.get("/projects/{project_id}/kaisen/members")
def get_member_limits(project_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    """Per-member spend limits and today's spend. Owners see every member; a member sees only their own row."""
    _, member = projects.get_project_for_member(db, project_id, user)
    return {"project_id": project_id, "can_edit": member.role == "owner",
            "members": kmembers.describe(db, project_id, user.id, member.role == "owner")}


@router.put("/projects/{project_id}/kaisen/members/{user_id}")
def put_member_limits(project_id: int, user_id: int, payload: MemberLimitsIn, db: Session = Depends(get_db),
                      user: models.User = Depends(get_active_user)):
    projects.get_project_for_member(db, project_id, user, owner=True)
    if projects.get_membership(db, project_id, user_id) is None:
        raise HTTPException(status_code=404, detail="We can't find that member in this project.")
    data = payload.model_dump(exclude_unset=True)
    try:
        kmembers.save(db, project_id, user_id, user.id, data)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    audit.record(db, user, "kaisen.member_limits_update", "project", project_id,
                 f"member={user_id} " + " ".join(f"{k}={'none' if v is None else v}" for k, v in sorted(data.items())))
    return next(m for m in kmembers.describe(db, project_id, user.id, True) if m["user_id"] == user_id)


@router.post("/projects/{project_id}/kaisen/test/{what}")
def test_settings(what: str, project_id: int, db: Session = Depends(get_db),
                  user: models.User = Depends(get_active_user),
                  model_factory=Depends(get_model_factory), github_factory=Depends(get_github_factory)):
    projects.get_project_for_member(db, project_id, user)
    rs = ksettings.load(db, project_id)
    try:
        if what == "anthropic":
            if not rs.anthropic_api_key:
                return {"ok": False, "message": "No Anthropic API key is saved for this project yet."}
            model_factory(rs.anthropic_api_key).retrieve_model(rs.model)
            return {"ok": True, "message": "Key valid. Selected model available. " + ksettings.web_note(rs)}
        if what == "github":
            if not rs.github_token:
                return {"ok": False, "message": "No GitHub token is saved for this project yet."}
            return {"ok": True, "message": "A project GitHub token is saved. Builds that choose it can push; "
                                           "use Test on a build's Output card to check a repository."}
    except (ModelError, GitHubError) as exc:
        return {"ok": False, "message": redact(str(exc), rs.secret_values())}
    raise HTTPException(status_code=404, detail=NOT_FOUND)


# ---------------------------------------------------------------- builds

class OutputIn(BaseModel):
    """Per-build output (design section 10). Every field optional on PUT; secrets are write-only."""
    model_config = ConfigDict(extra="forbid")
    mode: Optional[str] = Field(default=None, max_length=16)
    repo: Optional[str] = Field(default=None, max_length=300)
    branch: Optional[str] = Field(default=None, max_length=100)
    push_on_finish: Optional[bool] = None
    open_pr: Optional[bool] = None
    auth: Optional[str] = Field(default=None, max_length=16)
    github_token: Optional[str] = Field(default=None, max_length=500)
    clear_github_token: bool = False


class BuildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: Optional[int] = None  # default: the caller's personal project
    name: str = Field(min_length=1, max_length=config.KAISEN_NAME_MAX)
    brief: str = Field(min_length=1, max_length=config.KAISEN_BRIEF_MAX)
    output: Optional[OutputIn] = None

    @field_validator("name", "brief")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("This can't be empty.")
        return v


class TextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=max(config.KAISEN_FEEDBACK_MAX, config.KAISEN_ANSWER_MAX))

    @field_validator("text")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Write something first.")
        return v


def get_build(db: Session, build_id: int, user: models.User, *, include_deleted: bool = False) -> KaisenBuild:
    """The build if the caller is a member of its project; otherwise 404 (no existence leak)."""
    b = db.get(KaisenBuild, build_id)
    if b is None or (b.deleted_at is not None and not include_deleted) or projects.get_membership(db, b.project_id, user.id) is None:
        raise HTTPException(status_code=404, detail="We can't find that build.")
    projects.forbid_if_suspended(db, db.get(models.Project, b.project_id))  # writes only; reads stay open
    return b


def _secrets(db: Session, b: KaisenBuild) -> list[str]:
    return ksettings.load_for_build(db, b).secret_values()


def _progress(messages: list | None) -> dict:
    """Model turns so far and the last tool it called, for the build page's progress strip."""
    step, last_tool = 0, None
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "assistant":
            step += 1
            for blk in m.get("content") or []:
                if isinstance(blk, dict) and blk.get("type") in ("tool_use", "server_tool_use"):
                    last_tool = blk.get("name")
    return {"step": step or None, "last_tool": last_tool}


def _tests_view(b: KaisenBuild, secrets_: list[str]) -> Optional[dict]:
    v = testresults.view(b)
    if v is not None:
        v["tail"] = redact(v["tail"], secrets_)  # secrets added after the result was stored
    return v


def _app_view(db: Session, b: KaisenBuild) -> Optional[dict]:
    a = deploy_mod.live_app(db, b)
    if a is None:
        return None
    return {"id": a.id, "name": a.name, "hostname": f"{a.hostname}.{config.APP_DOMAIN}", "status": a.status}


def _list_item(db: Session, b: KaisenBuild) -> dict:
    app = _app_view(db, b)
    last = builder.latest_run(db, b.id)
    creator = db.get(models.User, b.created_by)
    return {
        "id": b.id, "project_id": b.project_id, "name": b.name, "slug": b.slug, "status": b.status,
        "app_id": app["id"] if app else None,
        "app_hostname": app["hostname"] if app else None,
        "app_status": app["status"] if app else None,
        "file_count": db.scalar(select(func.count()).select_from(KaisenFile).where(KaisenFile.build_id == b.id)) or 0,
        "cost_usd": round(builder.spend_build(db, b.id), 6),
        "last_run_at": last.created_at.isoformat() if last else None,
        "last_error": redact(last.error, _secrets(db, b)) if last and last.error and last.status in ("error", "stopped") else None,
        "open_questions": db.scalar(select(func.count()).select_from(KaisenInbox).where(
            KaisenInbox.build_id == b.id, KaisenInbox.kind == "question", KaisenInbox.status == "open")) or 0,
        "created_by": {"id": creator.id, "email": creator.email} if creator else None,
        "created_at": b.created_at.isoformat(),
    }


def _detail(db: Session, b: KaisenBuild, request: Request) -> dict:
    out = _list_item(db, b)
    sec = _secrets(db, b)
    rs = ksettings.load_for_build(db, b)
    runs = db.scalars(select(KaisenRun).where(KaisenRun.build_id == b.id).order_by(KaisenRun.id.desc()).limit(10)).all()
    with_diff = snapshots.runs_with_diff(db, b.id)
    out.update({
        "brief": b.brief,
        "files": [{"path": p, "size": n} for p, n in builder.Workspace(db, b.id).list()],
        "runs": [{
            "id": r.id, "kind": r.kind, "status": r.status, "has_diff": r.id in with_diff,
            "summary": redact(r.summary, sec) if r.summary else None,
            "suggestions": [redact(x, sec) for x in (r.suggestions or [])],
            "error": redact(r.error, sec) if r.error else None,
            "cost_usd": round(r.cost_usd or 0.0, 6),
            **_progress(r.messages),
            "context_resets": builder.context_resets(r.messages or []),
            "review": reviewing.view(r, sec),
            "started_at": r.created_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "transcript": [{"role": role, "text": text} for role, text in transcript_lines(r.messages, sec)] if r.messages else None,
            "transcript_trimmed": not r.messages and r.status in ("done", "stopped", "error"),
        } for r in runs],
        "inbox": [{"id": i.id, "kind": i.kind, "text": redact(i.text, sec), "status": i.status,
                   "answer": redact(i.answer, sec) if i.answer else None, "created_at": i.created_at.isoformat()}
                  for i in db.scalars(select(KaisenInbox).where(KaisenInbox.build_id == b.id).order_by(KaisenInbox.id.desc()).limit(50))],
        "app": _app_view(db, b),
        "spend": {"build_usd": round(builder.spend_build(db, b.id), 6), "cap_build": rs.cap_build,
                  "month_usd": round(builder.spend_month(db, b.project_id), 6)},
        "output": ksettings.describe_output(b, rs),
        "tests": _tests_view(b, sec),
        "secrets": buildsecrets.describe(db, b.id),
        "has_anthropic_key": bool(rs.anthropic_api_key),
        "snapshots_kept": config.KAISEN_SNAPSHOTS_KEPT,
        "can_run": getattr(request.state, "api_scope", "full") != "read",
    })
    return out


@router.get("/kaisen/builds")
def list_builds(project_id: Optional[int] = None, db: Session = Depends(get_db),
                user: models.User = Depends(get_active_user)):
    if project_id is not None:
        projects.get_project_for_member(db, project_id, user)
        ids = [project_id]
    else:
        ids = [r[0] for r in projects.member_project_ids(db, user.id).all()]
    rows = db.scalars(select(KaisenBuild).where(KaisenBuild.project_id.in_(ids), KaisenBuild.deleted_at.is_(None))
                      .order_by(KaisenBuild.id.desc())).all()
    return [_list_item(db, b) for b in rows]


@router.post("/kaisen/builds", status_code=201)
def create_build(payload: BuildIn, request: Request, db: Session = Depends(get_db),
                 user: models.User = Depends(get_active_user)):
    if payload.project_id is None:
        project = projects.get_personal_project(db, user)
        projects.forbid_if_suspended(db, project)
    else:
        project, _ = projects.get_project_for_member(db, payload.project_id, user)
    projects.lock_project(db, project.id)
    n = db.scalar(select(func.count()).select_from(KaisenBuild).where(
        KaisenBuild.project_id == project.id, KaisenBuild.deleted_at.is_(None))) or 0
    if n >= config.KAISEN_MAX_BUILDS_PER_PROJECT:
        raise HTTPException(status_code=403, detail=(
            f"This project already has {n} builds, the most allowed. Delete one you no longer need."))
    b = KaisenBuild(project_id=project.id, created_by=user.id, name=payload.name, brief=payload.brief,
                    slug=builder.unique_slug(db, payload.name), status="idle",
                    output_mode="download", github_repo="", github_branch="", push_on_finish=True, open_pr=False,
                    github_auth="project")
    out_data = payload.output.model_dump(exclude_unset=True) if payload.output else {}
    if out_data:
        try:
            ksettings.apply_output(b, ksettings.load(db, project.id), out_data)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    db.add(b)
    db.commit()
    audit.record(db, user, "kaisen.build_create", "kaisen_build", b.id, f"project={project.id}")
    start_error = None
    if not ksettings.load(db, project.id).anthropic_api_key:
        start_error = ("This project has no Anthropic API key yet, so the build was created but not started. "
                       "A project owner can add one in Kaisen settings, then press Build.")
    else:
        run, msg = builder.start_build(db, b, user.id)
        if run is None:
            start_error = msg
        else:
            db.commit()
            audit.record(db, user, "kaisen.run", "kaisen_build", b.id, f"run={run.id} kind={run.kind}")
    if out_data:
        audit.record(db, user, "kaisen.output_update", "kaisen_build", b.id,
                     "fields=" + ",".join(ksettings.changed_fields(out_data)))
    out = _detail(db, b, request)
    out["start_error"] = start_error  # null when the first run was queued; else a plain reason (build stays idle)
    return out


@router.get("/kaisen/builds/{build_id}")
def read_build(build_id: int, request: Request, db: Session = Depends(get_db),
               user: models.User = Depends(get_active_user)):
    return _detail(db, get_build(db, build_id, user), request)


@router.delete("/kaisen/builds/{build_id}", status_code=204)
def delete_build(build_id: int, delete_app: bool = False, db: Session = Depends(get_db),
                 user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    app = deploy_mod.live_app(db, b)
    if app is not None and delete_app and app.status == "suspended":
        raise HTTPException(status_code=403, detail="This app is suspended by an administrator, so it can't be deleted here. "
                                                    "Contact an administrator.")
    now = _now()
    for r in db.scalars(select(KaisenRun).where(KaisenRun.build_id == b.id)).all():
        if r.status in builder.ACTIVE:
            builder.set_status(db, r, "stopped")
        r.messages, r.pending = [], None
    db.query(KaisenFile).filter(KaisenFile.build_id == b.id).delete()
    db.query(KaisenBundle).filter(KaisenBundle.build_id == b.id).delete()
    db.query(KaisenInbox).filter(KaisenInbox.build_id == b.id).delete()
    db.query(KaisenSecret).filter(KaisenSecret.build_id == b.id).delete()
    db.query(KaisenSnapshot).filter(KaisenSnapshot.build_id == b.id).delete()
    if app is not None:
        app.kaisen_build_id = None
        if delete_app:
            app.deleted_at = datetime.now(timezone.utc)
    b.app_id, b.deleted_at = None, now
    db.commit()
    audit.record(db, user, "kaisen.build_delete", "kaisen_build", build_id,
                 f"delete_app={bool(app is not None and delete_app)}")
    if app is not None and delete_app:
        audit.record(db, user, "app.delete", "app", app.id, "via=kaisen")
    return None


def _require_key(db: Session, b: KaisenBuild) -> None:
    if not ksettings.load(db, b.project_id).anthropic_api_key:
        raise HTTPException(status_code=422, detail=(
            "This project has no Anthropic API key yet. A project owner can add one in Kaisen settings."))


@router.post("/kaisen/builds/{build_id}/run")
def run_build(build_id: int, request: Request, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    _require_key(db, b)
    projects.lock_project(db, b.project_id)
    run, msg = builder.start_build(db, b, user.id)
    if run is None:
        raise HTTPException(status_code=409, detail=msg)
    db.commit()
    audit.record(db, user, "kaisen.run", "kaisen_build", b.id, f"run={run.id} kind={run.kind}")
    return _detail(db, b, request)


@router.post("/kaisen/builds/{build_id}/feedback")
def feedback(build_id: int, payload: TextIn, request: Request, db: Session = Depends(get_db),
             user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    if len(payload.text) > config.KAISEN_FEEDBACK_MAX:
        raise HTTPException(status_code=422, detail=f"Keep the change request under {config.KAISEN_FEEDBACK_MAX} characters.")
    _require_key(db, b)
    if builder.latest_run(db, b.id) is None:
        raise HTTPException(status_code=409, detail="Run the build first, then ask for changes.")
    projects.lock_project(db, b.project_id)
    app = _app_view(db, b)
    run, msg = builder.start_feedback(db, b, payload.text, f"https://{app['hostname']}" if app else None, user.id)
    if run is None:
        raise HTTPException(status_code=409, detail=msg)
    db.commit()
    audit.record(db, user, "kaisen.run", "kaisen_build", b.id, f"run={run.id} kind=feedback")
    return _detail(db, b, request)


@router.post("/kaisen/builds/{build_id}/inbox/{item_id}/answer")
def answer(build_id: int, item_id: int, payload: TextIn, request: Request, db: Session = Depends(get_db),
           user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    item = db.get(KaisenInbox, item_id)
    if item is None or item.build_id != b.id:
        raise HTTPException(status_code=404, detail="We can't find that question.")
    run = db.get(KaisenRun, item.run_id) if item.run_id else None
    if item.kind != "question" or item.status != "open" or run is None or run.status != "waiting":
        raise HTTPException(status_code=409, detail="That question was already answered or is no longer waiting.")
    if len(payload.text) > config.KAISEN_ANSWER_MAX:
        raise HTTPException(status_code=422, detail=f"Keep the answer under {config.KAISEN_ANSWER_MAX} characters.")
    builder.answer_question(db, item, payload.text)
    db.commit()
    return _detail(db, b, request)


@router.post("/kaisen/builds/{build_id}/inbox/{item_id}/dismiss")
def dismiss(build_id: int, item_id: int, request: Request, db: Session = Depends(get_db),
            user: models.User = Depends(get_active_user)):
    """Close a notice (deploy/push/error/budget/refusal) for everyone in the project. Questions are answered, not dismissed."""
    b = get_build(db, build_id, user)
    item = db.get(KaisenInbox, item_id)
    if item is None or item.build_id != b.id:
        raise HTTPException(status_code=404, detail="We can't find that message.")
    if item.kind == "question" and item.status == "open":
        raise HTTPException(status_code=409, detail="Answer the question instead; Kaisen is waiting for it.")
    item.status = "closed"
    db.commit()
    return _detail(db, b, request)


@router.post("/kaisen/builds/{build_id}/deploy")
def deploy(build_id: int, request: Request, db: Session = Depends(get_db), user: models.User = Depends(get_active_user),
           registry=Depends(get_registry)):
    b = get_build(db, build_id, user)
    try:
        deploy_mod.deploy_build(db, b, registry, force=True)
    except deploy_mod.DeployError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    return _detail(db, b, request)


@router.get("/kaisen/builds/{build_id}/files/{path:path}")
def read_file(build_id: int, path: str, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    try:
        norm = builder.normalize_path(path)
    except builder.ToolError:
        raise HTTPException(status_code=404, detail="We can't find that file.")
    row = db.scalar(select(KaisenFile).where(KaisenFile.build_id == b.id, KaisenFile.path == norm))
    if row is None:
        raise HTTPException(status_code=404, detail="We can't find that file.")
    return {"path": row.path, "size": row.size, "content": redact(row.content, _secrets(db, b)),
            "binary": False, "truncated": False}


# ---------------------------------------------------------------- restore points (H5)

def _get_snapshot(db: Session, b: KaisenBuild, sid: int) -> KaisenSnapshot:
    snap = db.get(KaisenSnapshot, sid)
    if snap is None or snap.build_id != b.id:
        raise HTTPException(status_code=404, detail="We can't find that restore point.")
    return snap


def _load_snapshot(snap: KaisenSnapshot) -> dict:
    try:
        return snapshots.load(snap)
    except snapshots.SnapshotError:
        raise HTTPException(status_code=409, detail="That restore point can't be read.")


def _diff_response(db: Session, b: KaisenBuild, old: dict, new: dict, frm: str, to: str) -> dict:
    return {"from": frm, "to": to, **snapshots.diff(old, new, _secrets(db, b))}


@router.get("/kaisen/builds/{build_id}/snapshots")
def list_snapshots(build_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    nums = snapshots.run_numbers(db, b.id)
    return [{"id": x.id, "run_id": x.run_id, "run_number": nums.get(x.run_id) if x.run_id else None, "kind": x.kind,
             "created_at": x.created_at.isoformat(), "files_count": x.files_count, "bytes": x.bytes}
            for x in snapshots.listing(db, b.id)]


@router.get("/kaisen/builds/{build_id}/snapshots/{snapshot_id}/diff")
def snapshot_diff(build_id: int, snapshot_id: int, db: Session = Depends(get_db),
                  user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    snap = _get_snapshot(db, b, snapshot_id)
    return _diff_response(db, b, _load_snapshot(snap), snapshots.current_files(db, b.id), "snapshot", "current")


@router.get("/kaisen/builds/{build_id}/runs/{run_id}/diff")
def run_diff(build_id: int, run_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    """What a run changed: the snapshot taken at its start against the next snapshot, or the current files."""
    b = get_build(db, build_id, user)
    start = db.scalars(select(KaisenSnapshot).where(
        KaisenSnapshot.build_id == b.id, KaisenSnapshot.run_id == run_id).order_by(KaisenSnapshot.id)).first()
    if start is None:
        raise HTTPException(status_code=404, detail="We can't find a restore point for that run.")
    nxt = db.scalars(select(KaisenSnapshot).where(
        KaisenSnapshot.build_id == b.id, KaisenSnapshot.id > start.id).order_by(KaisenSnapshot.id)).first()
    num = snapshots.run_numbers(db, b.id).get(run_id)
    return _diff_response(db, b, _load_snapshot(start), _load_snapshot(nxt) if nxt else snapshots.current_files(db, b.id),
                          f"before run #{num}", f"after run #{num}")


@router.post("/kaisen/builds/{build_id}/snapshots/{snapshot_id}/restore")
def restore_snapshot(build_id: int, snapshot_id: int, db: Session = Depends(get_db),
                     user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    snap = _get_snapshot(db, b, snapshot_id)
    projects.lock_project(db, b.project_id)
    last = builder.latest_run(db, b.id)
    if last is not None and last.status in builder.ACTIVE:
        raise HTTPException(status_code=409, detail="A run is in progress; stop it before restoring.")
    try:
        count, before_id = snapshots.restore(db, b.id, snap)
    except snapshots.SnapshotError:
        db.rollback()
        raise HTTPException(status_code=409, detail="That restore point can't be read.")
    db.commit()
    audit.record(db, user, "kaisen.restore", "kaisen_build", b.id,
                 f"snapshot={snap.id} files={count} before={before_id}")
    return {"restored": True, "files_count": count, "before_restore_snapshot_id": before_id}


@router.get("/kaisen/builds/{build_id}/download")
def download(build_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    files = [(r.path, r.content) for r in db.scalars(select(KaisenFile).where(KaisenFile.build_id == b.id))]
    if not files:
        raise HTTPException(status_code=409, detail="The build has no files yet.")
    runs = db.scalars(select(KaisenRun).where(KaisenRun.build_id == b.id).order_by(KaisenRun.id)).all()
    files = [f for f in files if f[0] != export.CONVERSATION_FILE]
    files.append((export.CONVERSATION_FILE, export.build_conversation(b.name, b.brief, runs, _secrets(db, b))))
    return Response(export.build_tarball(b.slug, files, export.build_mtime(db, b)), media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="{b.slug}.tar.gz"', "Cache-Control": "no-store"})


@router.post("/kaisen/builds/{build_id}/push")
def push(build_id: int, request: Request, db: Session = Depends(get_db), user: models.User = Depends(get_active_user),
         github_factory=Depends(get_github_factory)):
    b = get_build(db, build_id, user)
    rs = ksettings.load_for_build(db, b)
    out = ksettings.output_for(b, rs)
    if not out.push_enabled:
        raise HTTPException(status_code=422, detail=(
            "Pushing to GitHub is not set up for this build. Choose GitHub under Output, with a repository and a token."))
    try:
        msg = pushing.push_build(db, b, out, github_factory)
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=redact(str(exc), rs.secret_values()))
    db.add(KaisenInbox(build_id=b.id, kind="push", text=msg, status="closed"))
    db.commit()
    audit.record(db, user, "kaisen.push", "kaisen_build", b.id, f"repo={out.repo}")
    return _detail(db, b, request)


class SecretIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(max_length=config.KAISEN_SECRET_VALUE_MAX)


@router.get("/kaisen/builds/{build_id}/secrets")
def list_secrets(build_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    """Names only; values are masked and never returned."""
    return buildsecrets.describe(db, get_build(db, build_id, user).id)


@router.put("/kaisen/builds/{build_id}/secrets/{name}")
def put_secret(build_id: int, name: str, payload: SecretIn, db: Session = Depends(get_db),
               user: models.User = Depends(get_active_user)):
    """Set or replace a secret. It reaches the preview app on the next deploy (press Deploy, or the builder deploys)."""
    b = get_build(db, build_id, user)
    try:
        created = buildsecrets.put(db, b.id, name, payload.value, user.id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    audit.record(db, user, "kaisen.secret_set", "kaisen_build", b.id, f"name={name} created={created}")
    return {"secrets": buildsecrets.describe(db, b.id), "applies_on_next_deploy": True}


@router.delete("/kaisen/builds/{build_id}/secrets/{name}")
def delete_secret(build_id: int, name: str, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    row = db.scalar(select(KaisenSecret).where(KaisenSecret.build_id == b.id, KaisenSecret.name == name))
    if row is None:
        raise HTTPException(status_code=404, detail="We can't find that secret.")
    db.delete(row)
    app = deploy_mod.live_app(db, b)
    if app is not None and app.status != "suspended":  # the running preview stops getting it: drop it from the app too
        gone = [v for v in app.env_vars if v.key == name and v.is_secret]
        for v in gone:
            app.env_vars.remove(v)
        if gone:
            app.status, app.status_detail = "pending", None
            app.reset_retry()
    db.commit()
    audit.record(db, user, "kaisen.secret_delete", "kaisen_build", b.id, f"name={name}")
    return {"secrets": buildsecrets.describe(db, b.id), "applies_on_next_deploy": False}


@router.get("/kaisen/builds/{build_id}/output")
def get_output(build_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    return ksettings.describe_output(b, ksettings.load(db, b.project_id))


@router.put("/kaisen/builds/{build_id}/output")
def put_output(build_id: int, payload: OutputIn, db: Session = Depends(get_db),
               user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    data = payload.model_dump(exclude_unset=True)
    rs = ksettings.load(db, b.project_id)
    try:
        ksettings.apply_output(b, rs, data)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    audit.record(db, user, "kaisen.output_update", "kaisen_build", b.id, "fields=" + ",".join(ksettings.changed_fields(data)))
    return ksettings.describe_output(b, rs)


@router.post("/kaisen/builds/{build_id}/output/test")
def test_output(build_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user),
                github_factory=Depends(get_github_factory)):
    b = get_build(db, build_id, user)
    rs = ksettings.load_for_build(db, b)
    out = ksettings.output_for(b, rs)
    if out.mode != "github":
        return {"ok": False, "message": "Output is set to download only. Choose GitHub first."}
    if not out.repo:
        return {"ok": False, "message": "Save a repository URL first."}
    if not out.token:
        return {"ok": False, "message": "No access token is available. Add one for this build or in the project's Kaisen settings."}
    try:
        return {"ok": True, "message": github_factory(out.token).check_repo(out.repo)}
    except GitHubError as exc:
        return {"ok": False, "message": redact(str(exc), rs.secret_values())}


@router.get("/kaisen/spend")
def spend(project_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    projects.get_project_for_member(db, project_id, user)
    rs = ksettings.load(db, project_id)
    return {"project_id": project_id, "month_usd": round(builder.spend_month(db, project_id), 6),
            "today_usd": round(builder.spend_today(db, project_id), 6), "cap_day": rs.cap_day}


# ---------------------------------------------------------------- public bundle download

@router.get("/kaisen/bundles/{token}.tar.gz", include_in_schema=False)
def bundle(token: str, request: Request, db: Session = Depends(get_db)):
    """Public: the preview pod downloads its code here. 256-bit token, only its hash is stored; looked up by hash
    and compared in constant time. Anything wrong looks the same (404)."""
    ratelimit.check_bundle_fetch(db, request)
    h = deploy_mod.token_hash(token)
    row = db.scalar(select(KaisenBundle).where(KaisenBundle.token_hash == h))
    if row is None or not hmac.compare_digest(row.token_hash.encode(), h.encode()):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return Response(row.data, media_type="application/gzip", headers={
        "X-Bundle-SHA256": row.sha256, "Cache-Control": "no-store"})
