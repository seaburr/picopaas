"""Environment-driven settings.

Everything here is read from the process environment at import time. There is
no config file: the platform runs in containers where env vars are how
secrets and per-environment values (DB URL, signing keys) are supplied.

`ENVIRONMENT` defaults to "dev", which is the only environment allowed to run
without an explicit FERNET_KEY (SECRET_KEY is derived from it unless set). Anywhere else (staging,
production, CI-that-pretends-to-be-prod) those must be set or the app refuses
to start.
"""
import base64
import hashlib
import hmac
import os

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
IS_DEV = ENVIRONMENT == "dev"

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/picopaas.db")

# Insecure, fixed values used only when ENVIRONMENT=dev and nothing else is
# configured (local dev, tests). Never used outside of IS_DEV.
_DEV_SECRET_KEY = "dev-insecure-secret-key-do-not-use-outside-dev"
# Derived rather than a literal so secret scanners have nothing key-shaped to flag.
_DEV_FERNET_KEY = base64.urlsafe_b64encode(hashlib.sha256(_DEV_SECRET_KEY.encode()).digest()).decode()


def _required_outside_dev(env_var: str, dev_default: str) -> str:
    value = os.environ.get(env_var)
    if value:
        return value
    if IS_DEV:
        return dev_default
    raise RuntimeError(
        f"{env_var} must be set when ENVIRONMENT != 'dev' (current: {ENVIRONMENT!r})"
    )


FERNET_KEY = _required_outside_dev("FERNET_KEY", _DEV_FERNET_KEY)


def derive_secret_key(fernet_key: str) -> str:
    """JWT/HMAC signing key derived from the one platform key: hmac-sha256(FERNET_KEY, label), hex."""
    return hmac.new(fernet_key.encode(), b"picopaas-jwt-signing-v1", hashlib.sha256).hexdigest()


# FERNET_KEY is the only required secret. SECRET_KEY is derived from it unless set explicitly (back-compat).
# Rotating FERNET_KEY therefore also rotates JWT signing (everyone is logged out).
if os.environ.get("SECRET_KEY"):
    SECRET_KEY = os.environ["SECRET_KEY"]
elif os.environ.get("FERNET_KEY"):
    SECRET_KEY = derive_secret_key(FERNET_KEY)
else:
    SECRET_KEY = _DEV_SECRET_KEY  # dev only: FERNET_KEY already enforced above

# Comma-separated older Fernet keys, tried for decryption after FERNET_KEY (key rotation; see crypto.rotate_all).
FERNET_KEYS_PREVIOUS = tuple(k.strip() for k in os.environ.get("FERNET_KEYS_PREVIOUS", "").split(",") if k.strip())

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 7

# When "1", MySQLAdminClient talks to picopaas_ops stored procedures (deploy/mysql initdb) instead
# of issuing CREATE/GRANT/DROP directly. Required for the in-cluster picopaas_admin user (issue #1);
# leave unset for managed MySQL where the admin user holds real privileges.
MYSQL_ADMIN_USE_PROCEDURES = os.environ.get("MYSQL_ADMIN_USE_PROCEDURES") == "1"

# Path to a CA bundle for TLS to tenant MySQL servers (DO managed MySQL requires TLS). Empty = no TLS.
MYSQL_SSL_CA = os.environ.get("MYSQL_SSL_CA", "")

DEFAULT_APP_LIMIT = 1
API_KEY_PREFIX = "pp_sk_"

# Wildcard DNS domain apps are deployed under (ARCHITECTURE.md "DNS and TLS").
APP_DOMAIN = os.environ.get("APP_DOMAIN", "picopaas.xyz")

# --- Custom domains (user-attached domains + per-host auto-TLS; app/custom_domains.py + reconciler). Opt-in per app. ---
CUSTOM_DOMAINS_ENABLED = os.environ.get("CUSTOM_DOMAINS_ENABLED", "1") != "0"
# The CNAME target we tell users to point their domain at (the platform LB's DNS name). Verification requires the
# custom host to resolve to the same IP(s) as this name — so we accept a CNAME, an ALIAS, or a flattened apex.
CUSTOM_DOMAIN_CNAME_TARGET = os.environ.get("CUSTOM_DOMAIN_CNAME_TARGET", f"lb.{APP_DOMAIN}")
# Label prefixed to the custom host for the ownership TXT (e.g. _picopaas-challenge.blog.example.com). This is OUR
# ownership check (distinct from cert-manager's own _acme-challenge record used by the ACME HTTP-01 solver).
CUSTOM_DOMAIN_CHALLENGE_LABEL = os.environ.get("CUSTOM_DOMAIN_CHALLENGE_LABEL", "_picopaas-challenge")
# cert-manager ClusterIssuer (HTTP-01) that mints the per-host cert. DNS-01 issuers are DO-zone-only, so custom
# domains (outside our DO account) must use HTTP-01. Use the -staging issuer while testing to avoid LE rate limits.
CUSTOM_DOMAIN_ISSUER = os.environ.get("CUSTOM_DOMAIN_ISSUER", "letsencrypt-http01-prod")

# Host routing (app/main.py): the platform lives at PLATFORM_HOST, the apex serves the landing page, and
# LEGACY_HOSTS (comma-separated, default none) 301 to PLATFORM_HOST.
PLATFORM_HOST = os.environ.get("PLATFORM_HOST", "platform.picopaas.xyz").lower()
APEX_HOST = os.environ.get("APEX_HOST", "picopaas.xyz").lower()
LEGACY_HOSTS = tuple(
    h.strip().lower()
    for h in os.environ.get("LEGACY_HOSTS", "").split(",")
    if h.strip()
)

# Hard cap on (summed, compressed-layer) image size, enforced at app create/update
# via app/providers/registry.py (ARCHITECTURE.md §2 "Image size").
# Images whose USER is a name (not a number) can't be verified by kubelet's runAsNonRoot, so
# the pod is forced to run as this UID/GID instead (app.run_as_uid; see routers/apps.py).
NAMED_USER_RUN_AS_UID = 10001

# How often the leader re-resolves floating image tags (app/image_check.py::watch_all).
IMAGE_WATCH_MINUTES = int(os.environ.get("IMAGE_WATCH_MINUTES", 10))
MAX_IMAGE_SIZE_BYTES = int(os.environ.get("MAX_IMAGE_SIZE_BYTES", 512 * 1024 * 1024))

# Per-app MySQL user MAX_USER_CONNECTIONS (guards the shared DB server; managed max_connections is ~76 total).
# Applied at CREATE USER and re-applied (ALTER USER) to existing app users on each reconcile sweep.
# Apps with a connection pool must keep their pool size at or below this.
MAX_DB_USER_CONNECTIONS = int(os.environ.get("MAX_DB_USER_CONNECTIONS", "10"))

# PostgreSQL (managed, DO): per-role connection cap (the 1 GB node has ~22 usable connections in total), and the
# size multiple of DB_QUOTA_BYTES at which the quota poll escalates from stage 1 (default_transaction_read_only +
# kill sessions) to stage 2 (REVOKE CONNECT + kill sessions).
PG_MAX_DB_USER_CONNECTIONS = int(os.environ.get("PG_MAX_DB_USER_CONNECTIONS", "5"))
PG_HARD_CAP_RATIO = float(os.environ.get("PG_HARD_CAP_RATIO", "2.0"))
PG_STATEMENT_TIMEOUT_MS = int(os.environ.get("PG_STATEMENT_TIMEOUT_MS", "60000"))
PG_IDLE_TX_TIMEOUT_MS = int(os.environ.get("PG_IDLE_TX_TIMEOUT_MS", "60000"))
# Bootstrap of the managed-PG DbServer 'primary-pg' (BOOTSTRAP_PG_HOST/PORT/USER/PASSWORD env, see
# app/scheduler.py::bootstrap_pg_server). The CA is a file path (PEM) like MYSQL_SSL_CA; the password is NOT here.
PG_BOOTSTRAP_CA = os.environ.get("BOOTSTRAP_PG_CA", "")

# Hard ceiling on active apps per account, INCLUDING platform admins (Chas, 2026-10-20). A user's app_limit can be
# lower, never higher (see resources.effective_app_limit).
MAX_APPS_PER_USER = int(os.environ.get("MAX_APPS_PER_USER", "5"))

# Ceiling for the admin-set per-app replica count (PUT /admin/apps/{id}/replicas).
MAX_REPLICAS = int(os.environ.get("MAX_REPLICAS", "3"))

# --- Platform caps: SINGLE SOURCE OF TRUTH (Chas lowered them 2026-10-19; bootstrap.md pending his update) ---
# Used by k8s_manifests (container limits, LimitRange, quota = 2x), quota.py, resources.py, health.py.
DB_QUOTA_BYTES = int(os.environ.get("DB_QUOTA_BYTES", 50 * 1024 * 1024))
STORAGE_QUOTA_BYTES = int(os.environ.get("STORAGE_QUOTA_BYTES", 100 * 1024 * 1024))
# Quota poll cadence. DB is a cheap size query, and a runaway writer can add hundreds of MB in minutes, so poll
# every minute. Storage lists the bucket and swaps keys (heavier), so every 5 minutes.
DB_QUOTA_POLL_SECONDS = int(os.environ.get("DB_QUOTA_POLL_SECONDS", 60))
STORAGE_QUOTA_POLL_SECONDS = int(os.environ.get("STORAGE_QUOTA_POLL_SECONDS", 300))
# Per-app CPU/memory history (app_metric_samples): one row per app per interval, newest N kept per app.
METRICS_SAMPLE_SECONDS = int(os.environ.get("METRICS_SAMPLE_SECONDS", 60))
METRICS_RETENTION_POINTS = int(os.environ.get("METRICS_RETENTION_POINTS", 180))
DEFAULT_CPU_REQUEST_M = 64
DEFAULT_CPU_LIMIT_M = int(os.environ.get("DEFAULT_CPU_LIMIT_M", 200))
DEFAULT_MEM_REQUEST_MI = 128
DEFAULT_MEM_LIMIT_MI = int(os.environ.get("DEFAULT_MEM_LIMIT_MI", 384))
# Per-app scratch disk (container filesystem, /tmp, logs); a pod over it is evicted. Chas, 2026-10-19.
EPHEMERAL_REQUEST_MI = 64
EPHEMERAL_LIMIT_MI = int(os.environ.get("EPHEMERAL_LIMIT_MI", 250))

# Hard ceilings (and floors) for the platform-admin per-app overrides (PATCH /admin/apps/{id}/resources). Chas's numbers,
# except OVERRIDE_DB_QUOTA_MAX_MB (no ceiling was given; 1024 chosen). Defaults stay the constants above.
OVERRIDE_DB_CONNECTIONS_MIN, OVERRIDE_DB_CONNECTIONS_MAX = 1, 25
OVERRIDE_EPHEMERAL_MIN_MI, OVERRIDE_EPHEMERAL_MAX_MI = 64, 2048
OVERRIDE_DB_QUOTA_MIN_MB, OVERRIDE_DB_QUOTA_MAX_MB = 1, 1024
OVERRIDE_STORAGE_QUOTA_MIN_MB, OVERRIDE_STORAGE_QUOTA_MAX_MB = 1, 51200
OVERRIDE_IMAGE_SIZE_MIN_MB, OVERRIDE_IMAGE_SIZE_MAX_MB = 1, 2048
# Per-pod outbound bandwidth cap (Cilium bandwidth manager, pod annotation kubernetes.io/egress-bandwidth), in Mbit/s.
# Applies to everything the pod sends, including HTTP responses to visitors. Ceiling 160 Mbit/s = 20 MB/s.
EGRESS_BANDWIDTH_MBIT = int(os.environ.get("EGRESS_BANDWIDTH_MBIT", 20))
OVERRIDE_EGRESS_MIN_MBIT, OVERRIDE_EGRESS_MAX_MBIT = 1, 160
# User-set health-check probe timeout (seconds); unset = readiness 3s / liveness 5s.
HEALTH_TIMEOUT_MIN_S, HEALTH_TIMEOUT_MAX_S = 1, 90

SESSION_COOKIE_NAME = "session"
# Cookies are only marked Secure outside dev so local HTTP development still works.
SESSION_COOKIE_SECURE = not IS_DEV

# --- Reconciler / scheduler (app/scheduler.py) ---
RECONCILER_ENABLED = os.environ.get("RECONCILER_ENABLED") == "1"
# Where per-app NetworkPolicy allows MySQL egress: "in-cluster" (ns picopaas-data,
# pod app=mysql) or "cidr" (managed DB, MYSQL_EGRESS_CIDR).
MYSQL_EGRESS_MODE = os.environ.get("MYSQL_EGRESS_MODE", "cidr")
MYSQL_EGRESS_CIDR = os.environ.get("MYSQL_EGRESS_CIDR", "")
# Managed PostgreSQL tenant egress (port comes from the DbServer, 25060 on DO). Empty = reuse MYSQL_EGRESS_CIDR
# (same VPC CIDR). Postgres is always managed, so there is no in-cluster mode.
PG_EGRESS_CIDR = os.environ.get("PG_EGRESS_CIDR", "")

# Ingress controller (Traefik, standard Ingress API): class name for per-app Ingresses and the
# namespace the per-app NetworkPolicy admits traffic from.
INGRESS_CLASS = os.environ.get("INGRESS_CLASS", "traefik")
INGRESS_NAMESPACE = os.environ.get("INGRESS_NAMESPACE", "traefik")

# Node ceiling shown on the admin health dashboard (informational; the real cap is in Terraform).
MAX_NODES = int(os.environ["MAX_NODES"]) if os.environ.get("MAX_NODES", "").isdigit() else None

# A rollout still not ready after this many minutes fails (retryable) with a message saying what we saw.
DEPLOY_TIMEOUT_MINUTES = 10

# Brute-force protection on /auth/login and /auth/register (app/ratelimit.py). DB-backed so the limits are
# shared by every replica. RATE_LIMIT_ENABLED=0 turns it off (the test suite does; limiter tests turn it on).
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "1") != "0"
# The per-email login lockout is the real brute-force control and is IP-independent. The per-IP limits are a
# coarse floodwall: the DO LB runs in TCP mode without PROXY protocol, so the app currently sees the LB's
# address, not the client's — the IP buckets are effectively shared platform-wide. They're set generously so
# they never block a legitimate trickle; enabling PROXY protocol (see docs/ARCHITECTURE.md) would let us tighten
# them to real per-client values.
LOGIN_EMAIL_MAX_FAILS = 10       # failed logins per email per window: the primary control
LOGIN_IP_MAX_FAILS = 100         # per client IP (shared LB address today) — a floodwall, not the main control
LOGIN_WINDOW_SECONDS = 15 * 60
REGISTER_IP_MAX = 40             # signups per IP per window (shared LB address today); approval still gates every account
REGISTER_WINDOW_SECONDS = 60 * 60

# Optional managed-DB disk size (MB) so the Health page can show data/disk %. Unset = show data size only.
# The true disk capacity comes from DO's managed-DB metrics API (planned follow-up); do not guess it.
MANAGED_DB_DISK_MB = int(os.environ["MANAGED_DB_DISK_MB"]) if os.environ.get("MANAGED_DB_DISK_MB", "").isdigit() else None

# Managed-DB host metrics (cpu/mem/disk %) from DigitalOcean's scrapable-metrics endpoint
# (https://<db-host>:9273/metrics, basic auth from GET /v2/databases/metrics/credentials, uses DIGITALOCEAN_TOKEN).
# Scrape of DO's managed-DB Prometheus endpoint (host cpu/mem/disk). OFF by default: DO managed MySQL does not
# serve external scraping (documented limitation; use the control-panel Insights tab), so the scrape only
# ConnectErrors for us. Left behind the flag in case we change DB engine or DO adds support. Set
# DB_METRICS_ENABLED=1 to re-enable. No extra cost; also skipped when no token is set.
DB_METRICS_ENABLED = os.environ.get("DB_METRICS_ENABLED", "0") != "0"
DB_METRICS_PORT = int(os.environ.get("DB_METRICS_PORT", "9273"))
# Optional CA bundle for the metrics TLS cert; empty = reuse MYSQL_SSL_CA (same DO cluster CA), else system CAs.
DB_METRICS_CA = os.environ.get("DB_METRICS_CA", "")
