"""Kaisen file uploads (docs/design/kaisen.md section 16, security review 16.8). Raw-body uploads (no multipart), bytes
re-encoded in the secret-less image unit, per-project admin flag, takedown and hash blocklist for admins.

User routes live under /kaisen/builds/{id}/uploads and need membership of the build's project (non-members: 404).
Adding or replacing a file needs the platform kill switch AND the project flag; listing, reading and deleting never do."""
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app import audit, config, models, projects, ratelimit
from app.db import get_db
from app.dependencies import get_active_user, require_admin_session, require_platform_admin
from app.kaisen import builder, deploy as deploy_mod, secretscan, uploads as ku
from app.kaisen.models import KaisenAsset, KaisenBuild, KaisenBundle, KaisenUploadBlock
from app.providers.base import ImageProcessError, ImageProcessor
from app.routers.kaisen import get_build, require_kaisen

_processor: Optional[ImageProcessor] = None


def get_image_processor() -> ImageProcessor:
    """The real HTTP client to the image pod (tests override this with providers.fake.FakeImageProcessor)."""
    global _processor
    if _processor is None:
        from app.providers.imgproc import HttpImageProcessor
        _processor = HttpImageProcessor()
    return _processor


router = APIRouter(tags=["kaisen-uploads"], dependencies=[Depends(require_kaisen)])
admin_router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])

CSP_RASTER = "default-src 'none'; sandbox"
CSP_SVG = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"
_SHA = re.compile(r"[0-9a-f]{64}")
_IMG_STATUS = {"unsupported": 415, "corrupt": 422, "too_big": 422, "animated": 422, "timeout": 504, "busy": 503,
               "unavailable": 503}
_IMG_REASON = {"unsupported": "type", "corrupt": "corrupt", "too_big": "bomb", "animated": "bomb"}


# ---------------------------------------------------------------- views

def _item(r: KaisenAsset) -> dict:
    return {"id": r.id, "kind": r.kind, "path": r.path, "name": r.path.rsplit("/", 1)[-1], "name_orig": r.name_orig,
            "mime": r.mime, "ext": r.ext, "size": r.size, "width": r.width, "height": r.height,
            "description": r.description, "version": r.version, "run_id": r.run_id, "sha256": r.sha256,
            "created_by": r.created_by, "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat()}


def _list_out(db: Session, b: KaisenBuild, user: models.User) -> dict:
    return {"uploads": [_item(r) for r in ku.listing(db, b.id)], "quota": ku.quota(db, b, user.id),
            "notice": ku.PUBLIC_NOTICE, **ku.availability(db, b.project_id)}


# ---------------------------------------------------------------- gates

def write_ctx(build_id: int, request: Request, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    """Everything that must pass BEFORE the body is read: auth, membership, suspension, switches, rate limit (R3, R8)."""
    b = get_build(db, build_id, user)
    if not ku.platform_enabled(db):
        raise HTTPException(status_code=403, detail=ku.SWITCHED_OFF)
    if not ku.project_enabled(db, b.project_id):
        raise HTTPException(status_code=403, detail=ku.NOT_ENABLED)
    ratelimit.check_upload(db, request, user.id, b.project_id)  # counts rejected attempts too
    return b, user


def _no_active_run(db: Session, b: KaisenBuild) -> None:
    last = builder.latest_run(db, b.id)
    if last is not None and last.status in builder.ACTIVE:
        raise HTTPException(status_code=409, detail="Wait for the current run to finish or stop it first.")


async def _read_body(request: Request) -> bytes:
    cap = config.KAISEN_UPLOAD_MAX_BYTES_IN
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > cap:
                raise HTTPException(status_code=413, detail=_too_big(int(declared)))
        except ValueError:
            raise HTTPException(status_code=400, detail="Bad Content-Length.")
    buf = bytearray()
    async for chunk in request.stream():  # a running total: chunked bodies carry no Content-Length
        buf += chunk
        if len(buf) > cap:
            raise HTTPException(status_code=413, detail=_too_big())
    return bytes(buf)


def _too_big(size: int | None = None) -> str:
    seen = f"That file is {size / (1024 * 1024):.1f} MB. " if size else "That file is too large. "
    return f"{seen}The most allowed is {config.KAISEN_UPLOAD_MAX_BYTES_IN // (1024 * 1024)} MB; make it smaller and try again."


def _reject(db: Session, user: models.User, b: KaisenBuild, exc: ku.Refused) -> HTTPException:
    db.rollback()
    if exc.reason not in ("busy", "quota", "rule"):
        audit.record(db, user, "kaisen.upload_rejected", "kaisen_build", b.id, f"reason={exc.reason}")
    return HTTPException(status_code=exc.status, detail=exc.message)


# ---------------------------------------------------------------- ingest (runs in a worker thread)

def _process(db: Session, data: bytes, det: ku.Detected, processor: ImageProcessor):
    if not ku.gate.enter():
        raise ku.Refused(503, "Image processing is busy. Try again in a moment.", "busy")
    try:
        res = processor.process(data, det.family)
    except ImageProcessError as exc:
        raise ku.Refused(_IMG_STATUS.get(exc.code, 422), exc.message, _IMG_REASON.get(exc.code, "busy")) from None
    finally:
        ku.gate.leave()
    want = ("image/svg+xml",) if det.family == "svg" else ("image/png", "image/jpeg", "image/webp")
    if res.mime not in want or not ku.valid_output(res.mime, res.data):  # S5: never trust the unit's answer
        raise ku.Refused(422, "That image couldn't be processed.", "corrupt")
    return res


def _ingest(db: Session, b: KaisenBuild, user: models.User, kind: str, filename: str, description: Optional[str],
            data: bytes, processor: ImageProcessor, replace: Optional[KaisenAsset]) -> dict:
    try:
        orig = ku.sha256(data)
        if ku.blocked(db, orig):
            raise ku.Refused(422, ku.BLOCKED, "blocklist")
        det = ku.sniff(data, kind, filename)
        notes: list[str] = []
        width = height = None
        if det.family == "text":
            text = ku.decode_text(data)
            if (hit := secretscan.find(text)):
                raise ku.Refused(422, secretscan.refusal_for_upload(*hit), "secret")
            stored, mime, ext = data, "text/plain", det.ext
        else:
            if det.family == "svg" and (hit := secretscan.find(data.decode("utf-8", "replace"))):
                raise ku.Refused(422, secretscan.refusal_for_upload(*hit), "secret")
            res = _process(db, data, det, processor)
            stored, mime, ext, width, height, notes = res.data, res.mime, ku.ext_for(res.mime), res.width, res.height, res.notes
            if ku.blocked(db, ku.sha256(stored)):
                raise ku.Refused(422, ku.BLOCKED, "blocklist")
        if replace is not None and ext != replace.ext:
            raise ku.Refused(415, f"A replacement must be the same type ({replace.ext}). Delete the file and upload the new one.", "type")
        projects.lock_project(db, b.project_id)
        ku.check_quota(db, b, user.id, kind, len(stored), replacing=replace)
        now = ku._now()
        if replace is None:
            path = ku.stored_path(db, b.id, kind, filename, ext)
            if kind == "asset" and ku.file_collision(db, b.id, path):
                raise ku.Refused(409, f"A generated file already uses {path}. Ask Kaisen to remove or rename it, then upload again.", "rule")
            row = KaisenAsset(build_id=b.id, kind=kind, path=path, name_orig=ku.clean_orig(filename), mime=mime, ext=ext,
                              size=len(stored), sha256=ku.sha256(stored), orig_sha256=orig, width=width, height=height,
                              description=ku.clean_description(description), version=1, data=stored, created_by=user.id,
                              created_at=now, updated_at=now)
            db.add(row)
        else:
            row = replace
            row.mime, row.size, row.sha256, row.orig_sha256, row.width, row.height = mime, len(stored), ku.sha256(stored), orig, width, height
            row.data, row.version, row.updated_at = stored, row.version + 1, now
            if filename:
                row.name_orig = ku.clean_orig(filename)
            if description is not None:
                row.description = ku.clean_description(description)
        db.commit()
    except ku.Refused as exc:
        raise _reject(db, user, b, exc) from None
    audit.record(db, user, "kaisen.upload_replace" if replace is not None else "kaisen.upload_add", "kaisen_build", b.id,
                 f"path={row.path} kind={kind} size={row.size} sha256={row.sha256[:12]}")
    return {**_item(row), "notes": notes}


def _check_description(db: Session, b: KaisenBuild, user: models.User, description: Optional[str]) -> None:
    if description and (found := ku.scan_text(description)):
        raise _reject(db, user, b, ku.Refused(422, f"The description looks like it contains {found}. Remove it.", "secret"))


# ---------------------------------------------------------------- user routes

@router.get("/kaisen/builds/{build_id}/uploads")
def list_uploads(build_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    return _list_out(db, get_build(db, build_id, user), user)


@router.post("/kaisen/builds/{build_id}/uploads", status_code=201)
async def create_upload(request: Request, kind: str = Query(...), description: str = Query("", max_length=2000),
                        filename: str = Query("", max_length=300), ctx=Depends(write_ctx), db: Session = Depends(get_db),
                        processor: ImageProcessor = Depends(get_image_processor)):
    """Body = the raw file (Content-Type application/octet-stream). `kind` is asset or reference; `filename` is a label only."""
    b, user = ctx
    if kind not in config.KAISEN_UPLOAD_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of: {', '.join(config.KAISEN_UPLOAD_KINDS)}.")
    _check_description(db, b, user, description)
    data = await _read_body(request)
    return await run_in_threadpool(_ingest, db, b, user, kind, filename, description, data, processor, None)


@router.put("/kaisen/builds/{build_id}/uploads/{upload_id}")
async def replace_upload(upload_id: int, request: Request, description: Optional[str] = Query(None, max_length=2000),
                         filename: str = Query("", max_length=300), ctx=Depends(write_ctx), db: Session = Depends(get_db),
                         processor: ImageProcessor = Depends(get_image_processor)):
    """Body = the raw replacement file. Keeps the stored name and kind, version + 1; the type must stay the same."""
    b, user = ctx
    row = ku.get_row(db, b.id, upload_id, with_data=True)
    if row is None:
        raise HTTPException(status_code=404, detail="We can't find that upload.")
    _no_active_run(db, b)
    _check_description(db, b, user, description)
    data = await _read_body(request)
    return await run_in_threadpool(_ingest, db, b, user, row.kind, filename, description, data, processor, row)


class DescriptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(max_length=config.KAISEN_UPLOAD_DESC_MAX)


@router.patch("/kaisen/builds/{build_id}/uploads/{upload_id}")
def describe_upload(build_id: int, upload_id: int, payload: DescriptionIn, db: Session = Depends(get_db),
                    user: models.User = Depends(get_active_user)):
    b = get_build(db, build_id, user)
    row = ku.get_row(db, b.id, upload_id)
    if row is None:
        raise HTTPException(status_code=404, detail="We can't find that upload.")
    _check_description(db, b, user, payload.description)
    row.description, row.updated_at = ku.clean_description(payload.description), ku._now()
    db.commit()
    audit.record(db, user, "kaisen.upload_describe", "kaisen_build", b.id, f"path={row.path}")
    return _item(row)


@router.delete("/kaisen/builds/{build_id}/uploads/{upload_id}", status_code=204)
def delete_upload(build_id: int, upload_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    """Members can always remove their uploads (also in a suspended project, also with the switches off). Bundles that
    held the bytes are deleted too (R4): the next deploy builds a fresh one."""
    b = get_build(db, build_id, user, allow_suspended=True)
    row = ku.get_row(db, b.id, upload_id)
    if row is None:
        raise HTTPException(status_code=404, detail="We can't find that upload.")
    _no_active_run(db, b)
    path, sha = row.path, row.sha256
    db.delete(row)
    db.query(KaisenBundle).filter(KaisenBundle.build_id == b.id).delete()
    db.commit()
    audit.record(db, user, "kaisen.upload_delete", "kaisen_build", b.id, f"path={path} sha256={sha[:12]}")
    return Response(status_code=204)


@router.get("/kaisen/builds/{build_id}/uploads/{upload_id}/content")
def upload_content(build_id: int, upload_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    """The stored bytes to the owner, never inline-executable: nosniff, a locked CSP, no cache; references are text/plain
    attachments. The filename in Content-Disposition is the stored name, never the one the user typed."""
    b = get_build(db, build_id, user)
    row = ku.get_row(db, b.id, upload_id, with_data=True)
    if row is None:
        raise HTTPException(status_code=404, detail="We can't find that upload.")
    name = row.path.rsplit("/", 1)[-1]
    h = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store", "ETag": f'"{row.sha256}"'}
    if row.kind == "reference":
        h.update({"Content-Security-Policy": CSP_RASTER, "Content-Disposition": f'attachment; filename="{name}"'})
        return Response(row.data, media_type="text/plain; charset=utf-8", headers=h)
    h["Content-Security-Policy"] = CSP_SVG if row.mime == "image/svg+xml" else CSP_RASTER
    h["Content-Disposition"] = f'inline; filename="{name}"'
    return Response(row.data, media_type=row.mime, headers=h)


# ---------------------------------------------------------------- admin routes

class FlagIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _project_or_404(db: Session, project_id: int) -> models.Project:
    p = projects.live_project(db, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="We can't find that project.")
    return p


@admin_router.get("/projects/{project_id}/kaisen-uploads")
def get_project_flag(project_id: int, db: Session = Depends(get_db)):
    p = _project_or_404(db, project_id)
    return {"project_id": p.id, "name": p.name, "kaisen_uploads_enabled": bool(p.kaisen_uploads_enabled)}


@admin_router.put("/projects/{project_id}/kaisen-uploads")
def set_project_flag(project_id: int, payload: FlagIn, db: Session = Depends(get_db),
                     admin: models.User = Depends(require_platform_admin)):
    """Turn Kaisen file uploads on or off for one project (the only way to change the flag)."""
    p = _project_or_404(db, project_id)
    p.kaisen_uploads_enabled = payload.enabled
    db.commit()
    audit.record(db, admin, "kaisen.uploads_flag", "project", p.id, f"project={p.id} enabled={payload.enabled}")
    return {"project_id": p.id, "name": p.name, "kaisen_uploads_enabled": bool(p.kaisen_uploads_enabled)}


@admin_router.get("/kaisen/uploads", dependencies=[Depends(require_admin_session)])  # lists user emails: web UI only
def search_uploads(sha256: Optional[str] = Query(None), build_id: Optional[int] = Query(None), limit: int = Query(100, ge=1, le=500),
                   db: Session = Depends(get_db)):
    """Find uploads by hash (stored or as-received) and/or build, across all builds. No bytes."""
    q = select(KaisenAsset).options(defer(KaisenAsset.data)).order_by(KaisenAsset.id.desc()).limit(limit)
    if sha256 is not None:
        h = sha256.strip().lower()
        if not _SHA.fullmatch(h):
            raise HTTPException(status_code=422, detail="sha256 must be 64 hex characters.")
        q = q.where((KaisenAsset.sha256 == h) | (KaisenAsset.orig_sha256 == h))
    if build_id is not None:
        q = q.where(KaisenAsset.build_id == build_id)
    out = []
    for r in db.scalars(q):
        b = db.get(KaisenBuild, r.build_id)
        creator = db.get(models.User, r.created_by) if r.created_by else None
        out.append({**_item(r), "orig_sha256": r.orig_sha256, "build_id": r.build_id, "project_id": b.project_id if b else None,
                    "build_deleted": bool(b is None or b.deleted_at is not None),
                    "created_by_email": creator.email if creator else None})
    return out


@admin_router.get("/kaisen/uploads/summary")
def uploads_summary(db: Session = Depends(get_db)):
    total, cap = ku.platform_bytes(db), config.KAISEN_UPLOAD_PLATFORM_BYTES
    pct = round(100 * total / cap, 1)
    return {"platform_bytes": total, "platform_limit_bytes": cap, "percent": pct,
            "warn": pct >= config.KAISEN_UPLOAD_PLATFORM_WARN_PERCENT,
            "files": db.query(KaisenAsset).count(), "blocklist_entries": db.query(KaisenUploadBlock).count()}


@admin_router.delete("/kaisen/builds/{build_id}/uploads/{upload_id}")
def takedown_upload(build_id: int, upload_id: int, reason: str = Query(..., min_length=1, max_length=200),
                    suspend_app: bool = Query(False), db: Session = Depends(get_db),
                    admin: models.User = Depends(require_platform_admin)):
    """Takedown: the bytes are deleted, both hashes are blocklisted (exact match only), every bundle of the build is
    deleted (so the old public bundle URL 404s) and the build's preview is flagged for redeploy; optionally the
    preview app is suspended in the same action."""
    row = ku.get_row(db, build_id, upload_id)
    if row is None:
        raise HTTPException(status_code=404, detail="We can't find that upload.")
    b = db.get(KaisenBuild, build_id)
    sha = row.sha256
    ku.takedown(db, row, admin, reason.strip())
    suspended = False
    app = deploy_mod.live_app(db, b) if b is not None else None
    if suspend_app and app is not None and app.status != "suspended":
        projects.suspend_app_row(app, "admin", admin, f"Upload taken down: {reason.strip()}"[:500])
        suspended = True
    db.commit()
    audit.record(db, admin, "kaisen.upload_takedown", "kaisen_build", build_id,
                 f"sha256={sha[:12]} upload={upload_id} app_suspended={suspended} reason={reason.strip()}")
    return {"taken_down": True, "build_id": build_id, "sha256": sha, "app_suspended": suspended, "needs_redeploy": app is not None}
