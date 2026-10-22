from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.health_path import DEFAULT_HEALTH_PATH, validate_health_path

from app.env_refs import EnvValidationError, validate_env_key

MASKED_VALUE = "********"

# Containers run non-root with all capabilities dropped, so they can't bind privileged ports (<1024).
PORT_DESC = "Container port, 1024-65535 (non-root containers can't bind privileged ports below 1024)."

# One password policy for register, change-password and admin reset.
PASSWORD_MIN = 8
PASSWORD_MAX = 256


# --- Auth ---

class UserRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    email: EmailStr
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    accept_terms: bool

    @field_validator("accept_terms")
    @classmethod
    def _must_accept(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("Accept the Privacy Policy and Acceptable Use to continue (send accept_terms: true).")
        return v


class UserLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    email: EmailStr
    password: str


class ChangePassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(max_length=PASSWORD_MAX)
    new_password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)


class TemporaryPasswordOut(BaseModel):
    temporary_password: str


API_KEY_NAME_MAX = 64


API_KEY_SCOPES = ("read", "full")


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    # 'full' (default) = everything the account can do; 'read' = read-only (safe methods only). Two tiers (Chas).
    scope: Literal["read", "full"] = "full"

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v):
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > API_KEY_NAME_MAX:
            raise ValueError(f"Name must be {API_KEY_NAME_MAX} characters or fewer.")
        if not v.isprintable():
            raise ValueError("Name can only contain printable characters.")
        return v


class ApiKeyCreateOut(BaseModel):
    """Returned only once, at creation time — the raw key is never stored or shown again."""

    id: int
    key: str
    prefix: str
    name: Optional[str] = None
    scope: str = "full"
    created_at: datetime


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    prefix: str
    name: Optional[str] = None
    scope: str = "full"
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    role: str
    app_limit: int
    status: str
    created_at: datetime
    must_change_password: bool = False


class Features(BaseModel):
    databases: bool
    storage: bool


class LimitOverride(BaseModel):
    cpu_min_m: int
    cpu_max_m: int
    mem_min_mi: int
    mem_max_mi: int


class Limits(BaseModel):
    cpu_limit_m: int
    mem_limit_mi: int
    cpu_request_m: int
    mem_request_mi: int
    image_mb: int
    db_mb: int
    bucket_mb: int
    ephemeral_mi: int
    override: LimitOverride


class MeOut(UserOut):
    max_apps: int
    app_limit_effective: int
    features: Features
    limits: Limits


# --- Env vars ---

class EnvVarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    key: str
    value: str
    is_secret: bool = False
    undecryptable: bool = Field(default=False, exclude=True)  # echoed back from EnvVarOut; ignored

    @field_validator("key")
    @classmethod
    def _validate_key(cls, v: str) -> str:
        try:
            validate_env_key(v)
        except EnvValidationError as exc:
            raise ValueError(str(exc)) from exc
        return v


class EnvVarOut(BaseModel):
    key: str
    value: str
    is_secret: bool
    undecryptable: bool = False  # stored ciphertext unreadable (platform key changed); re-enter the value


# --- Apps ---

class AppCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    name: str = Field(min_length=1, max_length=255)
    image: str = Field(min_length=1, max_length=512)
    port: int = Field(ge=1024, le=65535, description=PORT_DESC)
    want_db: bool = False
    db_engine: Literal["mysql", "pg"] = "mysql"  # which engine hosts the database when want_db (immutable later)
    want_storage: bool = False
    env: List[EnvVarIn] = Field(default_factory=list)
    health_check_enabled: bool = False
    health_check_path: str = Field(default=DEFAULT_HEALTH_PATH, max_length=255)
    auto_update: bool = True

    @field_validator("health_check_path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        return validate_health_path(v)


class AppUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    image: Optional[str] = Field(default=None, min_length=1, max_length=512)
    port: Optional[int] = Field(default=None, ge=1024, le=65535, description=PORT_DESC)
    env: Optional[List[EnvVarIn]] = None
    health_check_enabled: Optional[bool] = None
    health_check_path: Optional[str] = Field(default=None, max_length=255)
    auto_update: Optional[bool] = None

    @field_validator("health_check_path")
    @classmethod
    def _validate_path(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else validate_health_path(v)


class ResourcesOut(BaseModel):
    cpu_request_m: int
    cpu_limit_m: int
    mem_request_mi: int
    mem_limit_mi: int
    overridden: bool = False
    exceeds_platform_caps: bool = False


class UsageOut(BaseModel):
    size_bytes: int
    limit_bytes: int
    over_quota: bool


class AppQuotaOut(BaseModel):
    """Last polled usage (stored rows, no live calls). null = the app has no such resource yet."""
    db: Optional[UsageOut] = None
    storage: Optional[UsageOut] = None


class AppEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    at: datetime
    kind: str  # disk | evicted | oom | crashed
    message: str


class ImageVersionOut(BaseModel):
    digest: str
    image_ref: str
    created_at: datetime
    current: bool = False


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # keep the "forbid everywhere" invariant (pentest NEW-1)
    digest: str = Field(min_length=1, max_length=80)


class CustomDomainSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=255)


class CustomDomainOut(BaseModel):
    """Status + the exact DNS records the user must create (shown in the UI)."""
    host: str
    status: str  # pending | active | error
    verified_at: Optional[datetime] = None
    error: Optional[str] = None
    challenge_host: str  # add a TXT here...
    challenge_token: str  # ...with this value (ownership proof)
    cname_target: str  # and a CNAME from `host` to this (points the domain at the platform LB)


class AppOut(BaseModel):
    id: int
    owner_id: int
    name: str
    hostname: str
    image: str
    image_digest: Optional[str] = None
    auto_update: bool = True
    image_checked_at: Optional[datetime] = None
    image_update_error: Optional[str] = None
    port: int
    want_db: bool
    db_engine: str = "mysql"
    want_storage: bool
    health_check_enabled: bool = False
    health_check_path: str = DEFAULT_HEALTH_PATH
    replicas: int = 1
    status: str
    status_detail: Optional[str] = None
    status_reason: Optional[str] = None  # machine code for status=error (image_pull, crash_loop, oom, not_ready, quota, ...)
    retry_at: Optional[datetime] = None  # when an errored app will next be retried (null = not retrying)
    created_at: datetime
    last_event: Optional[AppEventOut] = None  # newest restart event, only if under 24h old
    resources: Optional[ResourcesOut] = None
    quota: AppQuotaOut = Field(default_factory=AppQuotaOut)
    custom_domain: Optional[CustomDomainOut] = None
    platform_subdomain: Optional[str] = None  # admin-assigned fixed label under the platform domain
    platform_host: Optional[str] = None  # ...and its full host, e.g. blog.<APP_DOMAIN>
    env: List[EnvVarOut] = Field(default_factory=list)
