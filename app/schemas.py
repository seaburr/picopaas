from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.env_refs import EnvValidationError, validate_env_key

MASKED_VALUE = "********"


# --- Auth ---

class UserRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class UserLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    email: EmailStr
    password: str


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


# --- Env vars ---

class EnvVarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    key: str
    value: str
    is_secret: bool = False

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


# --- Apps ---

class AppCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    name: str = Field(min_length=1, max_length=255)
    image: str = Field(min_length=1, max_length=512)
    port: int = Field(gt=0, le=65535)
    want_db: bool = False
    want_storage: bool = False
    env: List[EnvVarIn] = Field(default_factory=list)


class AppUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    image: Optional[str] = Field(default=None, min_length=1, max_length=512)
    port: Optional[int] = Field(default=None, gt=0, le=65535)
    env: Optional[List[EnvVarIn]] = None


class AppOut(BaseModel):
    id: int
    owner_id: int
    name: str
    hostname: str
    image: str
    port: int
    want_db: bool
    want_storage: bool
    status: str
    status_detail: Optional[str] = None
    created_at: datetime
    env: List[EnvVarOut] = Field(default_factory=list)
