"""Kaisen (Preview): per-project settings and builds. Non-members get 404; settings edits are owner-only.

Everything 404s when Kaisen is off (KAISEN_ENABLED and the admin setting kaisen_enabled). Deploys go through the
platform's own spec path as the build's creator (app/kaisen/deploy.py): no PicoPaaS API key is involved."""
import hashlib
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
from app.kaisen import builder, deploy as deploy_mod, pushing, settings as ksettings
from app.kaisen.github import GitHubError, RealGitHub
from app.kaisen.llm import AnthropicClient, ModelError
from app.kaisen.models import (KaisenBuild, KaisenBundle, KaisenFile, KaisenInbox, KaisenRun)
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
    output_mode: Optional[str] = Field(default=None, max_length=16)
    github_repo: Optional[str] = Field(default=None, max_length=300)
    github_branch: Optional[str] = Field(default=None, max_length=100)
    github_token: Optional[str] = Field(default=None, max_length=500)
    clear_github_token: bool = False
    push_on_finish: Optional[bool] = None
    open_pr: Optional[bool] = None


@router.get("/projects/{project_id}/kaisen")
def get_settings(project_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    _, member = projects.get_project_for_member(db, project_id, user)
    return ksettings.describe(db, project_id, can_edit=member.role == "owner")


@router.put("/projects/{project_id}/kaisen")
def put_settings(project_id: int, payload: SettingsIn, db: Session = Depends(get_db),
                 user: models.User = Depends(get_active_user)):
    projects.get_project_for_member(db, project_id, user, owner=True)
    data = payload.model_dump(exclude_unset=True)
    try:
        ksettings.save(db, project_id, user.id, data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    audit.record(db, user, "kaisen.settings_update", "project", project_id,
                 "fields=" + ",".join(ksettings.changed_fields(data)))
    return ksettings.describe(db, project_id, can_edit=True)


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
            got = model_factory(rs.anthropic_api_key).retrieve_model(rs.model)
            return {"ok": True, "message": f"OK: the key works and can use {got}."}
        if what == "github":
            if not (rs.github_repo and rs.github_token):
                return {"ok": False, "message": "Save a repository URL and an access token first."}
            return {"ok": True, "message": github_factory(rs.github_token).check_repo(rs.github_repo)}
    except (ModelError, GitHubError) as exc:
        return {"ok": False, "message": redact(str(exc), rs.secret_values())}
    raise HTTPException(status_code=404, detail=NOT_FOUND)


# ---------------------------------------------------------------- builds

class BuildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: Optional[int] = None  # default: the caller's personal project
    name: str = Field(min_length=1, max_length=config.KAISEN_NAME_MAX)
    brief: str = Field(min_length=1, max_length=config.KAISEN_BRIEF_MAX)

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
    return b


def _secrets(db: Session, b: KaisenBuild) -> list[str]:
    return ksettings.load(db, b.project_id).secret_values()


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
    rs = ksettings.load(db, b.project_id)
    runs = db.scalars(select(KaisenRun).where(KaisenRun.build_id == b.id).order_by(KaisenRun.id.desc()).limit(10)).all()
    out.update({
        "brief": b.brief,
        "files": [{"path": p, "size": n} for p, n in builder.Workspace(db, b.id).list()],
        "runs": [{
            "id": r.id, "kind": r.kind, "status": r.status,
            "summary": redact(r.summary, sec) if r.summary else None,
            "error": redact(r.error, sec) if r.error else None,
            "cost_usd": round(r.cost_usd or 0.0, 6),
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
        "output": {"mode": rs.output_mode, "repo": rs.github_repo, "branch": rs.github_branch, "can_push": rs.push_enabled},
        "has_anthropic_key": bool(rs.anthropic_api_key),
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
    else:
        project, _ = projects.get_project_for_member(db, payload.project_id, user)
    projects.lock_project(db, project.id)
    n = db.scalar(select(func.count()).select_from(KaisenBuild).where(
        KaisenBuild.project_id == project.id, KaisenBuild.deleted_at.is_(None))) or 0
    if n >= config.KAISEN_MAX_BUILDS_PER_PROJECT:
        raise HTTPException(status_code=403, detail=(
            f"This project already has {n} builds, the most allowed. Delete one you no longer need."))
    b = KaisenBuild(project_id=project.id, created_by=user.id, name=payload.name, brief=payload.brief,
                    slug=builder.unique_slug(db, payload.name), status="idle")
    db.add(b)
    db.commit()
    audit.record(db, user, "kaisen.build_create", "kaisen_build", b.id, f"project={project.id}")
    return _detail(db, b, request)


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
    run, msg = builder.start_build(db, b)
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
    run, msg = builder.start_feedback(db, b, payload.text, f"https://{app['hostname']}" if app else None)
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


@router.get("/kaisen/builds/{build_id}/download")
def download(build_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    from app.kaisen import export
    b = get_build(db, build_id, user)
    files = [(r.path, r.content) for r in db.scalars(select(KaisenFile).where(KaisenFile.build_id == b.id))]
    if not files:
        raise HTTPException(status_code=409, detail="The build has no files yet.")
    return Response(export.build_tarball(b.slug, files), media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="{b.slug}.tar.gz"', "Cache-Control": "no-store"})


@router.post("/kaisen/builds/{build_id}/push")
def push(build_id: int, request: Request, db: Session = Depends(get_db), user: models.User = Depends(get_active_user),
         github_factory=Depends(get_github_factory)):
    b = get_build(db, build_id, user)
    rs = ksettings.load(db, b.project_id)
    if not rs.push_enabled:
        raise HTTPException(status_code=422, detail=(
            "Pushing to GitHub is not set up. A project owner can choose it under Output in Kaisen settings."))
    try:
        msg = pushing.push_build(db, b, rs, github_factory)
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=redact(str(exc), rs.secret_values()))
    db.add(KaisenInbox(build_id=b.id, kind="push", text=msg, status="closed"))
    db.commit()
    audit.record(db, user, "kaisen.push", "kaisen_build", b.id, f"repo={rs.github_repo}")
    return _detail(db, b, request)


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
