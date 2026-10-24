"""Curated, static catalog of one-click app templates (public, non-root images)."""
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import models
from app.dependencies import get_active_user


REPO_EXAMPLES = "https://github.com/seaburr/picopaas/tree/main/examples"
SALT_CMD = "openssl rand -base64 48 | tr -d '\\n'"
HEX_CMD = "openssl rand -hex 32"


class TemplateHealthCheck(BaseModel):
    path: str
    timeout_seconds: Optional[int] = None


class TemplateVolume(BaseModel):
    size_gi: int
    mount_path: str
    required: bool = False  # false: the app also runs without it (state is then lost on restart)


class TemplateSecret(BaseModel):
    key: str
    hint: str  # how to generate / what to put in it


class TemplateOut(BaseModel):
    id: str
    name: str
    description: str
    image: str
    port: int
    wants_db: bool
    db_engine: Literal["mysql", "pg"] = "mysql"
    wants_storage: bool
    env: Dict[str, str]
    secrets: List[TemplateSecret] = []  # prefilled as empty secret env vars; the user must set each value
    health_check: Optional[TemplateHealthCheck] = None  # None = health check off
    volume: Optional[TemplateVolume] = None
    notes: str
    docs_url: str | None = None  # absolute https URL (example README on GitHub)


_WP_SALTS = [
    "WORDPRESS_" + n
    for n in (
        "AUTH_KEY", "SECURE_AUTH_KEY", "LOGGED_IN_KEY", "NONCE_KEY",
        "AUTH_SALT", "SECURE_AUTH_SALT", "LOGGED_IN_SALT", "NONCE_SALT",
    )
]

CATALOG: List[TemplateOut] = [
    TemplateOut(
        id="ghost",
        name="Ghost (blog/CMS)",
        description="The Ghost publishing platform, preconfigured for PicoPaaS MySQL (TLS) and bucket storage. No secrets to set; finish setup at /ghost/.",
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
        notes="CPU-bound, so the first start is slow; bump CPU (up to 1000m) for sustained traffic. Sign-in verification email is off (SMTP is blocked).",
        docs_url=f"{REPO_EXAMPLES}/ghost",
    ),
    TemplateOut(
        id="wordpress",
        name="WordPress (nginx + PHP-FPM)",
        description="WordPress with a locked-down first-boot install, MySQL over TLS and an optional 1 GiB volume for the whole site.",
        image="ghcr.io/seaburr/picopaas-example-wordpress:latest",
        port=8080,
        wants_db=True,
        wants_storage=False,
        env={
            "WORDPRESS_DB_HOST": "${{ db.host }}",
            "WORDPRESS_DB_PORT": "${{ db.port }}",
            "WORDPRESS_DB_NAME": "${{ db.name }}",
            "WORDPRESS_DB_USER": "${{ db.user }}",
            "WORDPRESS_DB_PASSWORD": "${{ db.password }}",
            "WORDPRESS_DB_TLS": "${{ db.tls }}",
            "WP_URL": "${{ app.url }}",
            "WP_TITLE": "My site",
            "WP_ADMIN_USER": "admin",
            "WP_ADMIN_EMAIL": "you@example.com",
            "WP_PAGE_CACHE": "true",
        },
        secrets=[
            TemplateSecret(key=k, hint=f"Random string, generate with: {SALT_CMD}") for k in _WP_SALTS
        ],
        health_check=TemplateHealthCheck(path="/healthz", timeout_seconds=10),
        volume=TemplateVolume(size_gi=1, mount_path="/var/www/html"),
        notes=(
            "Change WP_ADMIN_EMAIL to your address. Set all eight WORDPRESS_* secrets and keep them stable (the app refuses to start if any is missing). "
            "The admin password is generated on first boot and printed once in the app logs. "
            "Without a volume (instances with volumes off) it still runs, but uploads and plugins live on scratch disk and are lost on restart. "
            "Ask an admin for CPU 500m: the 200m default makes the installer and dashboard slow."
        ),
        docs_url=f"{REPO_EXAMPLES}/wordpress",
    ),
    TemplateOut(
        id="directus",
        name="Directus (headless CMS)",
        description="Directus data platform on MySQL, with files stored in the bucket. The health check is off on purpose: cold start takes 90-110 s.",
        image="directus/directus:12.4.1",
        port=8055,
        wants_db=True,
        wants_storage=True,
        env={
            "DB_CLIENT": "mysql",
            "DB_HOST": "${{ db.host }}",
            "DB_PORT": "${{ db.port }}",
            "DB_DATABASE": "${{ db.name }}",
            "DB_USER": "${{ db.user }}",
            "DB_PASSWORD": "${{ db.password }}",
            "DB_SSL__CA": "${{ db.ca_cert }}",
            "DB_POOL__MIN": "0",
            "DB_POOL__MAX": "5",
            "ADMIN_EMAIL": "admin@example.com",
            "STORAGE_LOCATIONS": "s3",
            "STORAGE_S3_DRIVER": "s3",
            "STORAGE_S3_KEY": "${{ storage.access_key }}",
            "STORAGE_S3_SECRET": "${{ storage.secret_key }}",
            "STORAGE_S3_BUCKET": "${{ storage.bucket }}",
            "STORAGE_S3_REGION": "${{ storage.region }}",
            "STORAGE_S3_ENDPOINT": "${{ storage.endpoint }}",
            "PUBLIC_URL": "${{ app.url }}",
            "TELEMETRY": "false",
            "HOME": "/tmp",
            "PM2_HOME": "/tmp/.pm2",
        },
        secrets=[
            TemplateSecret(key="KEY", hint=f"Random string, generate with: {HEX_CMD}. Keep it stable across deploys."),
            TemplateSecret(key="SECRET", hint=f"Random string, generate with: {HEX_CMD}. Keep it stable across deploys."),
            TemplateSecret(key="ADMIN_PASSWORD", hint="Password for the first admin (ADMIN_EMAIL)."),
        ],
        notes="Change ADMIN_EMAIL and set the three secrets. Expect about 90 s of downtime on every deploy or restart (no health check, so no restart loop).",
        docs_url=f"{REPO_EXAMPLES}/directus",
    ),
    TemplateOut(
        id="grafana",
        name="Grafana (dashboards)",
        description="Grafana with its state in PicoPaaS MySQL (TLS, encrypted but unverified).",
        image="grafana/grafana:13.2.4",
        port=3000,
        wants_db=True,
        wants_storage=False,
        env={
            "GF_SERVER_HTTP_PORT": "3000",
            "GF_SERVER_ROOT_URL": "${{ app.url }}",
            "GF_DATABASE_TYPE": "mysql",
            "GF_DATABASE_URL": "${{ db.url }}",
            "GF_DATABASE_SSL_MODE": "skip-verify",
            "GF_DATABASE_CA_CERT_PATH": "/etc/ssl/certs/ca-certificates.crt",
            "GF_DATABASE_MAX_OPEN_CONN": "8",
            "GF_DATABASE_MAX_IDLE_CONN": "2",
            "GF_SECURITY_ADMIN_USER": "admin",
            "GF_ANALYTICS_REPORTING_ENABLED": "false",
            "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        },
        secrets=[
            TemplateSecret(key="GF_SECURITY_ADMIN_PASSWORD", hint="Password for the admin user; pick a long one."),
        ],
        health_check=TemplateHealthCheck(path="/api/health"),
        notes="Set GF_SECURITY_ADMIN_PASSWORD, then sign in as admin. Outbound traffic is restricted, so only public data sources work.",
        docs_url=f"{REPO_EXAMPLES}/grafana",
    ),
    TemplateOut(
        id="n8n",
        name="n8n (workflow automation)",
        description="n8n on PostgreSQL (CA-verified TLS), trimmed to fit the memory and 50 MB database caps.",
        image="n8nio/n8n:2.41.7",
        port=5678,
        wants_db=True,
        db_engine="pg",
        wants_storage=False,
        env={
            "DB_TYPE": "postgresdb",
            "DB_POSTGRESDB_HOST": "${{ db.host }}",
            "DB_POSTGRESDB_PORT": "${{ db.port }}",
            "DB_POSTGRESDB_DATABASE": "${{ db.name }}",
            "DB_POSTGRESDB_USER": "${{ db.user }}",
            "DB_POSTGRESDB_PASSWORD": "${{ db.password }}",
            "DB_POSTGRESDB_SSL_ENABLED": "true",
            "DB_POSTGRESDB_SSL_CA": "${{ db.ca_cert }}",
            "DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED": "true",
            "DB_POSTGRESDB_POOL_SIZE": "2",
            "N8N_PROTOCOL": "https",
            "N8N_PORT": "5678",
            "WEBHOOK_URL": "${{ app.url }}",
            "N8N_EDITOR_BASE_URL": "${{ app.url }}",
            "GENERIC_TIMEZONE": "UTC",
            "N8N_USER_FOLDER": "/tmp",
            "NODE_OPTIONS": "--max-old-space-size=384",
            "EXECUTIONS_DATA_SAVE_ON_SUCCESS": "none",
            "EXECUTIONS_DATA_MAX_AGE": "72",
            "EXECUTIONS_DATA_PRUNE_MAX_COUNT": "1000",
            "N8N_PROXY_HOPS": "1",
            "N8N_COMMUNITY_PACKAGES_ENABLED": "false",
            "N8N_RUNNERS_ENABLED": "false",
            "N8N_DIAGNOSTICS_ENABLED": "false",
            "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
            "N8N_TEMPLATES_ENABLED": "false",
            "NODES_INCLUDE": '["n8n-nodes-base.webhook", "n8n-nodes-base.manualTrigger", "n8n-nodes-base.set", "n8n-nodes-base.httpRequest", "n8n-nodes-base.scheduleTrigger", "n8n-nodes-base.noOp", "n8n-nodes-base.code", "n8n-nodes-base.if", "n8n-nodes-base.respondToWebhook"]',
        },
        secrets=[
            TemplateSecret(
                key="N8N_ENCRYPTION_KEY",
                hint=f"Random string, generate with: {HEX_CMD}. Keep it stable, or every saved credential becomes unreadable.",
            ),
        ],
        health_check=TemplateHealthCheck(path="/healthz"),
        notes=(
            "Needs PostgreSQL, which an instance may have no capacity for. Raise memory to 512 MiB (admin) for comfort. "
            "Set N8N_ENCRYPTION_KEY and never change it. NODES_INCLUDE loads only common nodes; drop it to load all. "
            "Success executions are not kept, to stay inside the database quota."
        ),
        docs_url=f"{REPO_EXAMPLES}/n8n",
    ),
    TemplateOut(
        id="shlink",
        name="Shlink (URL shortener)",
        description="Shlink short links on MySQL with TLS. REST API only; the web client is a separate app.",
        image="shlinkio/shlink:5.1.8",
        port=8080,
        wants_db=True,
        wants_storage=False,
        env={
            "DB_DRIVER": "mysql",
            "DB_HOST": "${{ db.host }}",
            "DB_PORT": "${{ db.port }}",
            "DB_NAME": "${{ db.name }}",
            "DB_USER": "${{ db.user }}",
            "DB_PASSWORD": "${{ db.password }}",
            "DB_USE_ENCRYPTION": "true",
            "DEFAULT_DOMAIN": "short.example.com",
            "IS_HTTPS_ENABLED": "true",
            "WEB_WORKER_NUM": "4",
            "TASK_WORKER_NUM": "4",
        },
        secrets=[
            TemplateSecret(
                key="INITIAL_API_KEY",
                hint=f"Random string, generate with: {HEX_CMD}. Send it in the X-Api-Key header of the REST API.",
            ),
        ],
        health_check=TemplateHealthCheck(path="/rest/health"),
        notes=(
            "Set DEFAULT_DOMAIN to the bare hostname your app is served on (no https://, no path); ${{ app.url }} cannot be used here. "
            "Create the app first, then edit the value to match its hostname (or your reserved subdomain)."
        ),
        docs_url=f"{REPO_EXAMPLES}/shlink",
    ),
    TemplateOut(
        id="status",
        name="Status page (Gatus)",
        description="Uptime dashboard that probes public endpoints on a schedule. The shipped config is a generic demo (example.com + DNS check).",
        image="ghcr.io/seaburr/picopaas-example-status:latest",
        port=8080,
        wants_db=False,
        wants_storage=False,
        env={},
        notes=(
            "Fork examples/status and edit config.yaml with your own endpoints, then point the image at your build. "
            "History is in memory and resets on restart. It cannot probe this platform's own public URLs (load balancer hairpin)."
        ),
        docs_url=f"{REPO_EXAMPLES}/status",
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
]

router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("", response_model=List[TemplateOut])
def list_templates(_user: models.User = Depends(get_active_user)) -> List[TemplateOut]:
    return CATALOG
