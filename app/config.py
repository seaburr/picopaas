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
APP_DOMAIN = os.environ.get("APP_DOMAIN", "raisehellpraisedale.xyz")

# Host routing (app/main.py): the platform lives at PLATFORM_HOST, the apex serves the landing page, and
# LEGACY_HOSTS (comma-separated) 301 to PLATFORM_HOST.
PLATFORM_HOST = os.environ.get("PLATFORM_HOST", "platform.raisehellpraisedale.xyz").lower()
APEX_HOST = os.environ.get("APEX_HOST", "raisehellpraisedale.xyz").lower()
LEGACY_HOSTS = tuple(
    h.strip().lower()
    for h in os.environ.get("LEGACY_HOSTS", "picopaas.raisehellpraisedale.xyz").split(",")
    if h.strip()
)

# Hard cap on (summed, compressed-layer) image size, enforced at app create/update
# via app/providers/registry.py (ARCHITECTURE.md §2 "Image size").
# Images whose USER is a name (not a number) can't be verified by kubelet's runAsNonRoot, so
# the pod is forced to run as this UID/GID instead (app.run_as_uid; see routers/apps.py).
NAMED_USER_RUN_AS_UID = 10001

# How often the leader re-resolves floating image tags (app/image_check.py::watch_all).
IMAGE_WATCH_MINUTES = int(os.environ.get("IMAGE_WATCH_MINUTES", 10))
MAX_IMAGE_SIZE_BYTES = int(os.environ.get("MAX_IMAGE_SIZE_BYTES", 256 * 1024 * 1024))

# --- Platform caps: SINGLE SOURCE OF TRUTH (Chas lowered them 2026-10-19; bootstrap.md pending his update) ---
# Used by k8s_manifests (container limits, LimitRange, quota = 2x), quota.py, resources.py, health.py.
DB_QUOTA_BYTES = int(os.environ.get("DB_QUOTA_BYTES", 50 * 1024 * 1024))
STORAGE_QUOTA_BYTES = int(os.environ.get("STORAGE_QUOTA_BYTES", 100 * 1024 * 1024))
DEFAULT_CPU_REQUEST_M = 64
DEFAULT_CPU_LIMIT_M = int(os.environ.get("DEFAULT_CPU_LIMIT_M", 100))
DEFAULT_MEM_REQUEST_MI = 128
DEFAULT_MEM_LIMIT_MI = int(os.environ.get("DEFAULT_MEM_LIMIT_MI", 384))

SESSION_COOKIE_NAME = "session"
# Cookies are only marked Secure outside dev so local HTTP development still works.
SESSION_COOKIE_SECURE = not IS_DEV

# --- Reconciler / scheduler (app/scheduler.py) ---
RECONCILER_ENABLED = os.environ.get("RECONCILER_ENABLED") == "1"
# Where per-app NetworkPolicy allows MySQL egress: "in-cluster" (ns picopaas-data,
# pod app=mysql) or "cidr" (managed DB, MYSQL_EGRESS_CIDR).
MYSQL_EGRESS_MODE = os.environ.get("MYSQL_EGRESS_MODE", "cidr")
MYSQL_EGRESS_CIDR = os.environ.get("MYSQL_EGRESS_CIDR", "")

# Ingress controller (Traefik, standard Ingress API): class name for per-app Ingresses and the
# namespace the per-app NetworkPolicy admits traffic from.
INGRESS_CLASS = os.environ.get("INGRESS_CLASS", "traefik")
INGRESS_NAMESPACE = os.environ.get("INGRESS_NAMESPACE", "traefik")

# Node ceiling shown on the admin health dashboard (informational; the real cap is in Terraform).
MAX_NODES = int(os.environ["MAX_NODES"]) if os.environ.get("MAX_NODES", "").isdigit() else None

# A rollout still not ready after this many minutes fails (retryable) with a message saying what we saw.
DEPLOY_TIMEOUT_MINUTES = 10
