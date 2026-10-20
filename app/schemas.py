from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.health_path import DEFAULT_HEALTH_PATH, validate_health_path

from app.env_refs import EnvValidationError, validate_env_key

MASKED_VALUE = "********"

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


class ApiKeyCreateOut(BaseModel):
    """Returned only once, at creation time — the raw key is never stored or shown again."""

    id: int
    key: str
    prefix: str
    created_at: datetime


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    prefix: str
    created_at: datetime
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
    override: LimitOverride


class MeOut(UserOut):
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
    port: int = Field(gt=0, le=65535)
    want_db: bool = False
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
    port: Optional[int] = Field(default=None, gt=0, le=65535)
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
    want_storage: bool
    health_check_enabled: bool = False
    health_check_path: str = DEFAULT_HEALTH_PATH
    status: str
    status_detail: Optional[str] = None
    status_reason: Optional[str] = None  # machine code for status=error (image_pull, crash_loop, oom, not_ready, quota, ...)
    retry_at: Optional[datetime] = None  # when an errored app will next be retried (null = not retrying)
    created_at: datetime
    resources: Optional[ResourcesOut] = None
    env: List[EnvVarOut] = Field(default_factory=list)
