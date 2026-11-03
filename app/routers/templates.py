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
        image="ghcr.io/seaburr/picopaas-example-ghost6:latest",
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
        name="Grafana on MySQL",
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
            "GF_PLUGINS_PREINSTALL_DISABLED": "true",
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
        id="grafana-pg",
        name="Grafana on PostgreSQL",
        description="Grafana with its state in PicoPaaS PostgreSQL (TLS required).",
        image="grafana/grafana:13.2.4",
        port=3000,
        wants_db=True,
        db_engine="pg",
        wants_storage=False,
        env={
            "GF_SERVER_HTTP_PORT": "3000",
            "GF_SERVER_ROOT_URL": "${{ app.url }}",
            "GF_DATABASE_TYPE": "postgres",
            "GF_DATABASE_URL": "${{ db.url }}",
            "GF_DATABASE_SSL_MODE": "require",
            "GF_DATABASE_MAX_OPEN_CONN": "8",
            "GF_DATABASE_MAX_IDLE_CONN": "2",
            "GF_SECURITY_ADMIN_USER": "admin",
            "GF_ANALYTICS_REPORTING_ENABLED": "false",
            "GF_PLUGINS_PREINSTALL_DISABLED": "true",
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
            "N8N_USER_FOLDER": "/home/node",
            "N8N_DEFAULT_BINARY_DATA_MODE": "filesystem",
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
                hint=f"Random string, generate with: {HEX_CMD}. Keep it stable: it decrypts the saved credentials.",
            ),
        ],
        health_check=TemplateHealthCheck(path="/healthz"),
        volume=TemplateVolume(size_gi=1, mount_path="/home/node", required=True),
        notes=(
            "Needs PostgreSQL, which an instance may have no capacity for, and a volume (mounted at /home/node: it holds n8n's config, binary data and UI cache). Ask an admin to raise memory to 512 MiB: n8n is OOM-killed at the 384 MiB default. First boot takes a few minutes. "
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
        id="miniflux",
        name="Miniflux (RSS reader)",
        description="Minimalist feed reader with a web UI and REST API, on PostgreSQL. Public image, no custom build.",
        image="miniflux/miniflux:2.3.4",
        port=8080,
        wants_db=True,
        db_engine="pg",
        wants_storage=False,
        env={
            "DATABASE_URL": "postgres://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}?sslmode=verify-full&sslrootcert=${{ db.ca_file }}",
            "DATABASE_MAX_CONNS": "3",
            "DATABASE_MIN_CONNS": "1",
            "RUN_MIGRATIONS": "1",
            "CREATE_ADMIN": "1",
            "ADMIN_USERNAME": "admin",
            "BASE_URL": "${{ app.url }}",
            "POLLING_FREQUENCY": "60",
            "BATCH_SIZE": "25",
            "WORKER_POOL_SIZE": "2",
            "CLEANUP_ARCHIVE_READ_DAYS": "30",
            "CLEANUP_ARCHIVE_UNREAD_DAYS": "60",
        },
        secrets=[
            TemplateSecret(
                key="ADMIN_PASSWORD",
                hint="Password for the 'admin' user, created on first start. Change it later in Miniflux's Settings, not here.",
            ),
        ],
        health_check=TemplateHealthCheck(path="/healthcheck"),
        notes=(
            "Sign in as 'admin'. Article bodies are stored in the 50 MB database; the cleanup settings archive old entries. "
            "TLS to the database is CA-verified (verify-full against the platform CA file)."
        ),
        docs_url=f"{REPO_EXAMPLES}/miniflux",
    ),
    TemplateOut(
        id="forgejo",
        name="Forgejo (git forge)",
        description="Git hosting with issues and pull requests. Repos and SQLite live on a volume. Git over HTTPS only.",
        image="codeberg.org/forgejo/forgejo:17.0.1-rootless",
        port=3000,
        wants_db=False,
        wants_storage=False,
        env={
            "ROOT_URL": "${{ app.url }}",
            "HTTP_PORT": "3000",
            "DISABLE_SSH": "true",
            "DB_TYPE": "sqlite3",
            "FORGEJO__SERVER__START_SSH_SERVER": "false",
            "FORGEJO__SERVER__OFFLINE_MODE": "true",
            "FORGEJO__MAILER__ENABLED": "false",
            "FORGEJO__CRON_UPDATE_CHECKER__ENABLED": "false",
            "FORGEJO__ACTIONS__ENABLED": "false",
            "FORGEJO__SERVICE__DISABLE_REGISTRATION": "false",
        },
        secrets=[
            TemplateSecret(key="SECRET_KEY", hint=f"Random string, generate with: {HEX_CMD}. Also skips the install wizard."),
        ],
        health_check=TemplateHealthCheck(path="/api/healthz"),
        volume=TemplateVolume(size_gi=1, mount_path="/var/lib/gitea", required=True),
        notes=(
            "Register your account right after the first start: the first account becomes the admin. "
            "Then set FORGEJO__SERVICE__DISABLE_REGISTRATION to true. No SSH and no email."
        ),
        docs_url=f"{REPO_EXAMPLES}/forgejo",
    ),
    TemplateOut(
        id="memos",
        name="Memos (notes)",
        description="Lightweight notes and microblog on PostgreSQL. The official image plus USER 10001.",
        image="ghcr.io/seaburr/picopaas-example-memos:latest",
        port=5230,
        wants_db=True,
        db_engine="pg",
        wants_storage=False,
        env={
            "MEMOS_PORT": "5230",
            "MEMOS_DRIVER": "postgres",
            "MEMOS_DSN": "postgres://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}?sslmode=verify-full&sslrootcert=${{ db.ca_file }}",
            "MEMOS_INSTANCE_URL": "${{ app.url }}",
        },
        health_check=TemplateHealthCheck(path="/healthz"),
        notes=(
            "Create your account right after the first start: the first account becomes the admin. Then turn off sign-ups "
            "in Memos' settings. Attachments are stored in the 50 MB database unless you configure S3 storage."
        ),
        docs_url=f"{REPO_EXAMPLES}/memos",
    ),
    TemplateOut(
        id="vikunja",
        name="Vikunja (tasks)",
        description="To-do and project manager on PostgreSQL, with task attachments in the bucket. Public image, no custom build.",
        image="vikunja/vikunja:2.7.1",
        port=3456,
        wants_db=True,
        db_engine="pg",
        wants_storage=True,
        env={
            "HOME": "/tmp",
            "VIKUNJA_SERVICE_PUBLICURL": "${{ app.url }}/",
            "VIKUNJA_DATABASE_TYPE": "postgres",
            "VIKUNJA_DATABASE_HOST": "${{ db.host }}:${{ db.port }}",
            "VIKUNJA_DATABASE_DATABASE": "${{ db.name }}",
            "VIKUNJA_DATABASE_USER": "${{ db.user }}",
            "VIKUNJA_DATABASE_PASSWORD": "${{ db.password }}",
            "VIKUNJA_DATABASE_SSLMODE": "verify-full",
            "VIKUNJA_DATABASE_SSLROOTCERT": "${{ db.ca_file }}",
            "VIKUNJA_DATABASE_MAXOPENCONNECTIONS": "3",
            "VIKUNJA_DATABASE_MAXIDLECONNECTIONS": "2",
            "VIKUNJA_FILES_TYPE": "s3",
            "VIKUNJA_FILES_S3_ENDPOINT": "${{ storage.endpoint }}",
            "VIKUNJA_FILES_S3_BUCKET": "${{ storage.bucket }}",
            "VIKUNJA_FILES_S3_REGION": "${{ storage.region }}",
            "VIKUNJA_FILES_S3_ACCESSKEY": "${{ storage.access_key }}",
            "VIKUNJA_FILES_S3_SECRETKEY": "${{ storage.secret_key }}",
            "VIKUNJA_FILES_S3_USEPATHSTYLE": "false",
            "VIKUNJA_MAILER_ENABLED": "false",
        },
        secrets=[
            TemplateSecret(key="VIKUNJA_SERVICE_SECRET", hint=f"Random string (JWT signing key), generate with: {HEX_CMD}"),
        ],
        health_check=TemplateHealthCheck(path="/api/v1/info"),
        notes=(
            "Register your first user right after the first start, then add VIKUNJA_SERVICE_ENABLEREGISTRATION=false and redeploy "
            "so strangers cannot sign up (existing users keep working). No email, so password reset by mail is unavailable. "
            "TLS to the database is CA-verified."
        ),
        docs_url=f"{REPO_EXAMPLES}/vikunja",
    ),
    TemplateOut(
        id="umami",
        name="Umami (web analytics)",
        description="Privacy-friendly website analytics on PostgreSQL. Public image (postgresql-latest tag), no custom build.",
        image="ghcr.io/umami-software/umami:postgresql-latest",
        port=3000,
        wants_db=True,
        db_engine="pg",
        wants_storage=False,
        env={
            "HOME": "/tmp",
            "HOSTNAME": "0.0.0.0",
            "DATABASE_URL": "postgresql://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}?sslmode=verify-full&sslrootcert=${{ db.ca_file }}",
            "DISABLE_TELEMETRY": "1",
        },
        secrets=[
            TemplateSecret(key="APP_SECRET", hint=f"Random string (salt for session hashing), generate with: {HEX_CMD}"),
        ],
        health_check=TemplateHealthCheck(path="/api/heartbeat"),
        notes=(
            "WARNING: the default login is admin / umami. Sign in and change that password immediately, the app is public. "
            "Umami stores one database row per event, so the 50 MB database fills up quickly on a busy site; use it for small sites only. "
            "Memory peaks near 330 MiB during the first-boot migrations (cap is 384 MiB) and settles around 165-255 MiB."
        ),
        docs_url=f"{REPO_EXAMPLES}/umami",
    ),
    TemplateOut(
        id="ntfy",
        name="ntfy (push notifications)",
        description="Publish and subscribe push notifications over HTTP, with all state in PostgreSQL. Tiny Go binary (about 20 MiB RAM).",
        image="ghcr.io/seaburr/picopaas-example-ntfy:latest",
        port=8080,
        wants_db=True,
        db_engine="pg",
        wants_storage=False,
        env={
            "NTFY_LISTEN_HTTP": ":8080",
            "NTFY_BASE_URL": "${{ app.url }}",
            "NTFY_BEHIND_PROXY": "true",
            "NTFY_DATABASE_URL": "postgres://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}?sslmode=verify-full&sslrootcert=${{ db.ca_file }}&pool_max_conns=3",
            "NTFY_AUTH_DEFAULT_ACCESS": "deny-all",
            "NTFY_CACHE_DURATION": "12h",
        },
        secrets=[
            TemplateSecret(
                key="NTFY_AUTH_USERS",
                hint=(
                    "Comma-separated name:bcrypt-hash:role, e.g. tester:<hash>:admin. Make the hash with: "
                    "docker run --rm -i binwiederhier/ntfy user hash (it prompts for the password). "
                    "Bcrypt hashes contain $: single-quote the value in a shell or CLI."
                ),
            ),
        ],
        health_check=TemplateHealthCheck(path="/v1/health"),
        notes=(
            "Access defaults to deny-all, so nothing works until NTFY_AUTH_USERS holds at least one user (an admin can then create more and grant topics). "
            "Do not set cache-file, auth-file or web-push-file: all state is in the database."
        ),
        docs_url=f"{REPO_EXAMPLES}/ntfy",
    ),
    TemplateOut(
        id="it-tools",
        name="IT-Tools (developer utilities)",
        description="A static collection of handy developer tools (encoders, hashes, converters) that run in the browser. No database, no secrets.",
        image="ghcr.io/seaburr/picopaas-example-it-tools:latest",
        port=8080,
        wants_db=False,
        wants_storage=False,
        env={},
        health_check=TemplateHealthCheck(path="/"),
        notes="Nothing to configure. Everything runs client-side in the browser, so the app only serves static files and uses very little memory.",
        docs_url=f"{REPO_EXAMPLES}/it-tools",
    ),
    TemplateOut(
        id="excalidraw",
        name="Excalidraw (whiteboard)",
        description="Virtual whiteboard for sketching diagrams. Static site on nginx. No database, no secrets.",
        image="ghcr.io/seaburr/picopaas-example-excalidraw:latest",
        port=8080,
        wants_db=False,
        wants_storage=False,
        env={},
        health_check=TemplateHealthCheck(path="/"),
        notes="Nothing to configure. Drawing works fully in the browser. Live collaboration and the library browser call external services, which may not work from here.",
        docs_url=f"{REPO_EXAMPLES}/excalidraw",
    ),
    TemplateOut(
        id="vaultwarden",
        name="Vaultwarden (password manager)",
        description="Bitwarden-compatible server; vault and keys live on a volume. For test use, not passwords you need.",
        image="ghcr.io/seaburr/picopaas-example-vaultwarden:latest",
        port=8080,
        wants_db=False,
        wants_storage=False,
        env={
            "DOMAIN": "${{ app.url }}",
            "ROCKET_PORT": "8080",
            "SIGNUPS_ALLOWED": "true",
            "INVITATIONS_ALLOWED": "false",
            "SHOW_PASSWORD_HINT": "false",
        },
        health_check=TemplateHealthCheck(path="/alive"),
        volume=TemplateVolume(size_gi=1, mount_path="/data", required=True),
        notes=(
            "Create your account right after the first start, then set SIGNUPS_ALLOWED to false. "
            "No email, so use an authenticator app for two-factor login. The /admin page is off."
        ),
        docs_url=f"{REPO_EXAMPLES}/vaultwarden",
    ),
    TemplateOut(
        id="wikijs",
        name="Wiki.js 2 (wiki)",
        description="Wiki with Markdown editor and page history on PostgreSQL. Public image; needs an admin to raise DB connections to 10.",
        image="requarks/wiki:2.5",
        port=3000,
        wants_db=True,
        db_engine="pg",
        wants_storage=False,
        env={
            "DB_TYPE": "postgres",
            "DB_HOST": "${{ db.host }}",
            "DB_PORT": "${{ db.port }}",
            "DB_NAME": "${{ db.name }}",
            "DB_USER": "${{ db.user }}",
            "DB_PASS": "${{ db.password }}",
            "DB_SSL": "true",
            "DB_SSL_CA": "${{ db.ca_cert_body }}",
            "NODE_OPTIONS": "--max-old-space-size=300",
            "HOME": "/tmp",
        },
        volume=TemplateVolume(size_gi=1, mount_path="/wiki/data", required=True),
        notes=(
            "Ask an admin to set this app's DB connections to 10, or saving pages fails (Wiki.js pools up to 10). "
            "Finish the setup page right after the first start: whoever completes it becomes the admin."
        ),
        docs_url=f"{REPO_EXAMPLES}/wikijs",
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
