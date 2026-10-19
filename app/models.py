from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'uberadmin' (first registered user, unlimited apps) or 'user'.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    app_limit: Mapped[int] = mapped_column(default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)

    apps: Mapped[List["App"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    api_keys: Mapped[List["ApiKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def is_uberadmin(self) -> bool:
        return self.role == "uberadmin"


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # sha256 hex digest of the full raw key. The raw key is shown to the user
    # exactly once, at creation time, and never stored.
    hashed: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # Non-secret display prefix, e.g. "pp_sk_ab12cd34", so a user can tell
    # their keys apart without re-revealing the secret.
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    user: Mapped["User"] = relationship(back_populates="api_keys")


class App(Base):
    __tablename__ = "apps"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    image: Mapped[str] = mapped_column(String(512), nullable=False)
    port: Mapped[int] = mapped_column(nullable=False)
    want_db: Mapped[bool] = mapped_column(default=False, nullable=False)
    want_storage: Mapped[bool] = mapped_column(default=False, nullable=False)
    # No k8s calls happen yet; every app is created (and stays) 'pending'.
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    owner: Mapped["User"] = relationship(back_populates="apps")
    env_vars: Mapped[List["AppEnvVar"]] = relationship(
        back_populates="app", cascade="all, delete-orphan"
    )


class AppEnvVar(Base):
    __tablename__ = "app_env_vars"
    __table_args__ = (UniqueConstraint("app_id", "key", name="uq_app_env_vars_app_id_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Fernet token (ciphertext), always encrypted at rest regardless of
    # is_secret — is_secret only controls whether the plaintext is ever
    # returned to the API client.
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(default=False, nullable=False)

    app: Mapped["App"] = relationship(back_populates="env_vars")
