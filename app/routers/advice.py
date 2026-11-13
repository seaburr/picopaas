"""Health advice (rule-based, read-only): what to change on an app and why. Phase 1 of docs/research/health-advisor-2026-11-11.md.
Nothing here writes: an action only names an existing endpoint the frontend calls after a confirm."""
from typing import Any, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import advisor, models, projects
from app.db import get_db
from app.dependencies import get_active_user, require_platform_admin

router = APIRouter(prefix="/apps", tags=["advice"])
admin_router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])


@router.get("/{app_id}/advice")
def app_advice(app_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)) -> dict[str, Any]:
    """Findings for one app, critical first. Same access as the app's health reads: project members (and platform admins);
    anyone else gets 404. Admin-only rules are left out."""
    app = projects.get_app_for_member(db, app_id, user, allow_admin=True, detail="App not found.")
    facts = advisor.load_facts(db, [app])[app.id]
    return {"findings": [f.as_dict() for f in advisor.evaluate(facts)]}


@admin_router.get("/advice")
def admin_advice(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Findings across all live apps (batched queries), critical first then by app. Read-only: the only action is a link to
    the app's health page."""
    apps = (db.query(models.App).options(*advisor.LOAD_OPTIONS).filter(models.App.deleted_at.is_(None))
            .order_by(models.App.id).all())
    facts = advisor.load_facts(db, apps)
    emails = {u.id: u.email for u in db.query(models.User.id, models.User.email)
              .filter(models.User.id.in_({a.owner_id for a in apps})).all()}
    pids = {a.project_id for a in apps if a.project_id is not None}
    pnames = {p.id: p.name for p in db.query(models.Project.id, models.Project.name).filter(models.Project.id.in_(pids)).all()} if pids else {}
    rows = []
    for a in apps:
        for f in advisor.evaluate(facts[a.id], admin=True):
            d = f.as_dict()
            d["action"] = advisor.link("Open app", f"/apps/{a.id}")
            pid: Optional[int] = a.project_id
            rows.append({**d, "app_id": a.id, "app_name": a.name, "project_id": pid,
                         "project_name": pnames.get(pid) if pid is not None else None, "owner_email": emails.get(a.owner_id)})
    rows.sort(key=lambda r: (advisor.SEVERITY_ORDER[r["severity"]], r["app_id"], r["rule"]))
    return {"findings": rows}
