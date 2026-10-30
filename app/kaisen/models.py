"""Kaisen tables (docs/design/kaisen.md section 3). MySQL gets MEDIUMTEXT/LONGBLOB; SQLite falls back to Text/LargeBinary."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy import false as sa_false
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

MediumText = Text().with_variant(mysql.MEDIUMTEXT(), "mysql")
LongBlob = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")
MediumJSON = JSON().with_variant(mysql.JSON(), "mysql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class KaisenSettings(Base):
    __tablename__ = "kaisen_settings"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    anthropic_key_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    effort: Mapped[str] = mapped_column(String(16), nullable=False)
    cap_run: Mapped[float] = mapped_column(Float, nullable=False)
    cap_day: Mapped[float] = mapped_column(Float, nullable=False)
    cap_build: Mapped[float] = mapped_column(Float, nullable=False)
    github_repo: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    github_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    github_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    output_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="download", server_default="download")
    push_on_finish: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    open_pr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)


class KaisenBuild(Base):
    __tablename__ = "kaisen_builds"
    __table_args__ = (Index("ix_kaisen_builds_project_deleted", "project_id", "deleted_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    brief: Mapped[str] = mapped_column(MediumText, nullable=False)
    app_id: Mapped[Optional[int]] = mapped_column(ForeignKey("apps.id", use_alter=True, name="fk_kaisen_builds_app"), nullable=True)
    # idle | queued | running | waiting | done | stopped | error  (mirrors the latest run)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="idle")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class KaisenFile(Base):
    __tablename__ = "kaisen_files"
    __table_args__ = (UniqueConstraint("build_id", "path", name="uq_kaisen_file_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("kaisen_builds.id"), nullable=False)
    path: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(MediumText, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class KaisenRun(Base):
    __tablename__ = "kaisen_runs"
    __table_args__ = (Index("ix_kaisen_runs_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("kaisen_builds.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")  # queued|running|waiting|done|stopped|error
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="build")  # build | feedback | resume
    messages: Mapped[list] = mapped_column(MediumJSON, nullable=False, default=list)
    pending: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    lease_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class KaisenEvent(Base):
    __tablename__ = "kaisen_events"
    __table_args__ = (Index("ix_kaisen_events_project_at", "project_id", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("kaisen_runs.id"), nullable=False, index=True)
    build_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)  # no FK: events outlive a deleted build for 90 days
    project_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)


class KaisenInbox(Base):
    __tablename__ = "kaisen_inbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("kaisen_builds.id"), nullable=False, index=True)
    run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # question|budget|refusal|error|push|deploy
    text: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")  # open | closed
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)


class KaisenBundle(Base):
    __tablename__ = "kaisen_bundles"

    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("kaisen_builds.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[bytes] = mapped_column(LongBlob, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
