"""Curated, static catalog of one-click app templates (public, non-root images)."""
from typing import Dict, List

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import models
from app.dependencies import get_active_user


class TemplateOut(BaseModel):
    id: str
    name: str
    description: str
    image: str
    port: int
    wants_db: bool
    wants_storage: bool
    env: Dict[str, str]
    notes: str
    docs_url: str | None = None


CATALOG: List[TemplateOut] = [
    TemplateOut(
        id="ghost",
        name="Ghost (blog/CMS)",
        description="The Ghost publishing platform, preconfigured for PicoPaaS MySQL (TLS) and bucket storage.",
        image="ghcr.io/seaburr/picopaas-example-ghost:latest",
        port=2368,
        wants_db=True,
        wants_storage=True,
        env={
            "DB_HOST": "${{ db.host }}",
            "DB_PORT": "${{ db.port }}",
            "DB_NAME": "${{ db.name }}",
            "DB_USER": "${{ db.user }}",
            "DB_PASSWORD": "${{ db.password }}",
            "DB_CA_CERT": "${{ db.ca_cert }}",
            "S3_ENDPOINT": "${{ storage.endpoint }}",
            "S3_REGION": "${{ storage.region }}",
            "S3_BUCKET": "${{ storage.bucket }}",
            "S3_ACCESS_KEY": "${{ storage.access_key }}",
            "S3_SECRET_KEY": "${{ storage.secret_key }}",
            "SITE_URL": "${{ app.url }}",
        },
        notes="CPU-bound, so the first start is slow; bump CPU (up to 1000m) for sustained traffic. Needs a database and a bucket.",
        docs_url="/guide/recipes",
    ),
    TemplateOut(
        id="static-site",
        name="Static website (nginx)",
        description="nginx serving a default page. A starter: swap in your own image once it works.",
        image="nginxinc/nginx-unprivileged:alpine",
        port=8080,
        wants_db=False,
        wants_storage=False,
        env={},
        notes="Runs as non-root UID 101.",
    ),
    TemplateOut(
        id="podinfo",
        name="Sample API (podinfo)",
        description="A tiny Go service with a JSON API, health endpoints and metrics.",
        image="ghcr.io/stefanprodan/podinfo:6.15.0",
        port=9898,
        wants_db=False,
        wants_storage=False,
        env={},
        notes="Try /, /healthz and /version on your app URL.",
    ),
]

router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("", response_model=List[TemplateOut])
def list_templates(_user: models.User = Depends(get_active_user)) -> List[TemplateOut]:
    return CATALOG
