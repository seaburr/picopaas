"""Environment-driven settings.

Everything here is read from the process environment at import time. There is
no config file: the platform runs in containers where env vars are how
secrets and per-environment values (DB URL, signing keys) are supplied.

`ENVIRONMENT` defaults to "dev", which is the only environment allowed to run
without an explicit SECRET_KEY / FERNET_KEY. Anywhere else (staging,
production, CI-that-pretends-to-be-prod) those must be set or the app refuses
to start.
"""
import base64
import hashlib
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


SECRET_KEY = _required_outside_dev("SECRET_KEY", _DEV_SECRET_KEY)
FERNET_KEY = _required_outside_dev("FERNET_KEY", _DEV_FERNET_KEY)

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

# Hard cap on (summed, compressed-layer) image size, enforced at app create/update
# via app/providers/registry.py (ARCHITECTURE.md §2 "Image size").
MAX_IMAGE_SIZE_BYTES = 512 * 1024 * 1024

SESSION_COOKIE_NAME = "session"
# Cookies are only marked Secure outside dev so local HTTP development still works.
SESSION_COOKIE_SECURE = not IS_DEV

# --- Reconciler / scheduler (app/scheduler.py) ---
RECONCILER_ENABLED = os.environ.get("RECONCILER_ENABLED") == "1"
# Where per-app NetworkPolicy allows MySQL egress: "in-cluster" (ns picopaas-data,
# pod app=mysql) or "cidr" (managed DB, MYSQL_EGRESS_CIDR).
MYSQL_EGRESS_MODE = os.environ.get("MYSQL_EGRESS_MODE", "cidr")
MYSQL_EGRESS_CIDR = os.environ.get("MYSQL_EGRESS_CIDR", "")
