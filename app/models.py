from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy import false as sa_false
from sqlalchemy import true as sa_true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'platform_admin' (first registered user, unlimited apps) or 'user'.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    app_limit: Mapped[int] = mapped_column(default=1, nullable=False)
    # 'pending' (awaiting platform_admin approval) | 'active' | 'suspended' | 'deleted' (soft).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="active")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    terms_accepted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # Set by an admin password reset; cleared when the user changes their password.
    must_change_password: Mapped[bool] = mapped_column(default=False, server_default="0", nullable=False)
    # IANA timezone name for display; NULL = use the browser's timezone.
    timezone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Why/when/by whom an admin suspended the account (cleared on unsuspend); shown in the admin Suspensions list.
    suspended_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    suspended_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    suspended_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    suspended_from: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # status before suspension

    apps: Mapped[List["App"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    api_keys: Mapped[List["ApiKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def is_platform_admin(self) -> bool:
        return self.role == "platform_admin"


class PlatformInit(Base):
    """Singleton (id=1) claimed by the first registrant; its PK makes platform_admin assignment atomic."""
    __tablename__ = "platform_init"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    # sha256 hex digest of the full raw key. The raw key is shown to the user
    # exactly once, at creation time, and never stored.
    hashed: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # Non-secret display prefix, e.g. "pp_sk_ab12cd34", so a user can tell
    # their keys apart without re-revealing the secret.
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Access tier (Chas, 2026-10-20, KISS two-tier): 'read' = read-only (safe HTTP methods only),
    # 'full' = everything the owning account can do (what every key did before scopes existed; the default,
    # so pre-scope keys keep working). A key can never create or modify keys regardless of scope
    # (key management requires a browser session) — see app/dependencies.py and app/routers/api_keys.py.
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="full", server_default="full")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    user: Mapped["User"] = relationship(back_populates="api_keys")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    is_personal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    app_limit: Mapped[Optional[int]] = mapped_column(nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # Suspension (docs/design/suspensions.md): 'active' | 'suspended'. Writes by members are refused while suspended.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    suspended_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    suspended_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # admin user id
    suspended_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # True when the suspension came from suspending the owner of a personal project (user unsuspend lifts just that).
    suspended_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())

    members: Mapped[List["ProjectMember"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_member"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")  # owner | member
    added_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="members")


class ProjectInvite(Base):
    __tablename__ = "project_invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    invited_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class App(Base):
    __tablename__ = "apps"
    __table_args__ = (
        Index("ix_apps_deleted_status", "deleted_at", "status"),
        # Leads with owner_id, so it also serves as the FK index.
        Index("ix_apps_owner_deleted", "owner_id", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Creator of the app (kept for history/admin reports). Access is by project membership (app/projects.py).
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # Nullable until a later migration enforces it; every user has a personal project.
    project_id: Mapped[Optional[int]] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    # The Kaisen build this app was deployed from ("Built with Kaisen", Preview); NULL for ordinary apps.
    kaisen_build_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("kaisen_builds.id", use_alter=True, name="fk_apps_kaisen_build"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    image: Mapped[str] = mapped_column(String(512), nullable=False)
    # Set (NAMED_USER_RUN_AS_UID) when the image USER is a name; forces runAsUser/runAsGroup. None = numeric USER.
    run_as_uid: Mapped[Optional[int]] = mapped_column(nullable=True)
    # Manifest digest (sha256:...) the Deployment is pinned to; NULL until first resolved (then the plain image is rendered).
    image_digest: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    auto_update: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    image_checked_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)  # last successful registry resolve
    image_update_error: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Owner dismissed the current image_update_error; cleared when a different error is recorded (a new occurrence).
    image_update_error_dismissed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    # Digest that failed to start (auto-rollback) or was rolled away from (manual rollback): the watcher won't redeploy it.
    rejected_digest: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    port: Mapped[int] = mapped_column(nullable=False)
    want_db: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Which engine the app's database is placed on: 'mysql' (default) | 'pg'. Immutable once the database exists.
    db_engine: Mapped[str] = mapped_column(String(8), nullable=False, default="mysql", server_default="mysql")
    want_storage: Mapped[bool] = mapped_column(default=False, nullable=False)
    health_check_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    health_check_path: Mapped[str] = mapped_column(String(255), nullable=False, default="/healthz", server_default="/healthz")
    # Set by app/reconciler.py: 'pending' -> 'deploying' -> 'running', or 'error'
    # (see status_detail); 'deleted' once delete_app has torn everything down.
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    # Short, secret-free human-readable reason for status='error'. Never holds
    # raw provider exception text (could echo creds) — see app/reconciler.py.
    status_detail: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Short machine code for status='error' (image_pull, crash_loop, oom, not_ready, quota, unreadable_secrets, timeout, unknown, ...).
    status_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # When the app entered 'deploying' (reconciler's deploy timeout); NULL otherwise.
    deploying_since: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # Changed by POST /apps/{id}/restart; rendered as a pod-template annotation so the pod rolls.
    restart_nonce: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # sha256 of the last-applied flattened manifests (hash only; never the manifests). Lets the reconciler
    # re-apply running apps when the platform's rendering changes.
    rendered_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Why an app with status='suspended' is suspended: 'user' (its owner's account), 'project' (its project) or
    # 'admin' (this app on its own). Unsuspending a user/project restores only the apps it suspended.
    suspended_via: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    suspended_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    suspended_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    suspended_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # Reconciler retry/backoff for status='error' (see app/reconciler.py). Reset by success or any config change.
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # False when only a user change can fix the error (invalid env refs, unreadable secrets): never auto-retried.
    error_retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    # Custom domain (user-attached host + per-host auto-TLS; app/custom_domains.py). NULL = none, only the generated
    # *.APP_DOMAIN subdomain. custom_domain_status: 'pending' (DNS not yet verified) -> 'active' (verified, served with
    # its own cert) -> or 'error'. custom_domain_token is OUR ownership-TXT value (not the ACME challenge).
    custom_domain: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    # Admin-assigned fixed label under APP_DOMAIN (e.g. "blog" -> blog.<APP_DOMAIN>), served IN ADDITION to the
    # generated hostname on the same wildcard cert. Single DNS label; unique. See app/naming.py.
    platform_subdomain: Mapped[Optional[str]] = mapped_column(String(63), unique=True, nullable=True)
    custom_domain_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    custom_domain_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    custom_domain_error: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    custom_domain_verified_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # Platform admin resource overrides; NULL = platform default (app/resources.py).
    cpu_request_m: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_limit_m: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mem_request_mi: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mem_limit_mi: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    db_max_connections: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ephemeral_limit_mi: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    db_quota_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    storage_quota_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    image_size_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    egress_mbit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # User-set probe timeout in seconds (1..90); NULL = readiness 3s / liveness 5s.
    health_check_timeout_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Platform-admin-set Deployment replica count (1..config.MAX_REPLICAS); 0 is applied separately when suspended.
    replicas: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Persistent volume (app/volumes.py). NULL size = no volume. Grow-only; never detached (delete the app instead).
    volume_size_gi: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    volume_mount_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # PVC phase last seen by the reconciler ('Pending' | 'Bound' | ...); NULL until the PVC has been applied.
    volume_phase: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Measured usage (df of the mount, from a volume backup or a "stat" Job: app/volume_stat.py). NULL = never measured.
    volume_used_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    volume_used_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    volume_stat_job: Mapped[Optional[str]] = mapped_column(String(63), nullable=True)  # name of the stat Job in flight
    volume_stat_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)  # when the last stat Job was started
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    def reset_retry(self) -> None:
        """Forget error/backoff state (success, or a config change that re-queues the app)."""
        self.error_count = 0
        self.last_attempt_at = None
        self.error_retryable = True

    owner: Mapped["User"] = relationship(back_populates="apps")
    env_vars: Mapped[List["AppEnvVar"]] = relationship(
        back_populates="app", cascade="all, delete-orphan"
    )
    database: Mapped[Optional["AppDatabase"]] = relationship(
        back_populates="app", cascade="all, delete-orphan"
    )
    storage: Mapped[Optional["AppStorage"]] = relationship(
        back_populates="app", cascade="all, delete-orphan"
    )
    image_history: Mapped[List["AppImageHistory"]] = relationship(
        order_by="desc(AppImageHistory.created_at), desc(AppImageHistory.id)",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    events: Mapped[List["AppEvent"]] = relationship(
        order_by="desc(AppEvent.at), desc(AppEvent.id)", cascade="all, delete-orphan", passive_deletes=True
    )


class AppImageHistory(Base):
    """A digest the app successfully ran (reached 'running') on. Newest 5 per app are kept; used for rollback."""
    __tablename__ = "app_image_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True, nullable=False)
    digest: Mapped[str] = mapped_column(String(80), nullable=False)
    image_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    run_as_uid: Mapped[Optional[int]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)


class AppEvent(Base):
    """Something that happened to a running app (restart for disk/OOM/crash). Last 20 per app are kept."""
    __tablename__ = "app_events"
    __table_args__ = (
        UniqueConstraint("app_id", "dedupe_key", name="uq_app_events_dedupe"),
        Index("ix_app_events_app_at", "app_id", "at", "id"),  # leading app_id also serves as the FK index
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), nullable=False)
    at: Mapped[datetime] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # disk | evicted | oom | crashed
    message: Mapped[str] = mapped_column(String(255), nullable=False)
    # pod uid + restartCount/finishedAt: the same occurrence seen on many sweeps is stored once.
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    # Set when the owner dismisses this alert. A new occurrence is a new row (new dedupe_key), so it shows again.
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class AppMetricSample(Base):
    """One CPU/memory reading for an app (leader-sampled from the health snapshot). Newest N per app are kept."""
    __tablename__ = "app_metric_samples"
    __table_args__ = (Index("ix_app_metric_samples_app_ts", "app_id", "ts"),)  # leading app_id also serves as the FK index

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), nullable=False)
    ts: Mapped[datetime] = mapped_column(nullable=False)
    cpu_m: Mapped[int] = mapped_column(nullable=False)
    mem_mi: Mapped[int] = mapped_column(nullable=False)
    # cpu_m/mem_mi are SUMS over the app's pods at sample time. `pods` = how many pods were summed (old rows: 1);
    # `per_pod` = JSON {pod name: [cpu_m, mem_mi]} (null on old rows).
    pods: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
    per_pod: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AppEnvVar(Base):
    __tablename__ = "app_env_vars"
    __table_args__ = (UniqueConstraint("app_id", "key", name="uq_app_env_vars_app_id_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"), index=True, nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Fernet token (ciphertext), always encrypted at rest regardless of
    # is_secret — is_secret only controls whether the plaintext is ever
    # returned to the API client.
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(default=False, nullable=False)

    app: Mapped["App"] = relationship(back_populates="env_vars")


class DbServer(Base):
    """One MySQL instance (platform-managed or self-hosted) that can host app
    databases (ARCHITECTURE.md "Scaling"). `app/db_placement.py` picks which
    server a new `AppDatabase` lands on; `app/providers/mysql.py`'s real
    `MySQLAdmin` is constructed per-server from these fields."""

    __tablename__ = "db_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(default=3306, nullable=False)
    admin_user: Mapped[str] = mapped_column(String(255), nullable=False)
    # Fernet token (ciphertext) — the platform-admin password, via app/crypto.
    admin_password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Soft cap on how many AppDatabase rows this server should host; placement
    # (app/db_placement.py) treats it as the denominator for free-slot counts.
    max_databases: Mapped[int] = mapped_column(default=25, nullable=False)
    # 'active' (eligible for new placements), 'draining' (keep existing dbs,
    # no new ones), or 'full' (at/over max_databases).
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    # Database engine of this server: 'mysql' | 'pg' (selects the provider in app/providers/factory.py).
    engine: Mapped[str] = mapped_column(String(8), nullable=False, default="mysql", server_default="mysql")
    # PEM CA certificate for TLS-only servers (e.g. DO managed MySQL); exposed
    # to tenant apps as `${{ db.ca_cert }}`. NULL = plaintext server.
    ca_cert: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)

    databases: Mapped[List["AppDatabase"]] = relationship(back_populates="server")


class AppDatabase(Base):
    """The one MySQL database + user provisioned for an app that opted into
    `want_db` (ARCHITECTURE.md §2 "DB"). One-to-one with App."""

    __tablename__ = "app_databases"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"), unique=True, nullable=False)
    db_server_id: Mapped[int] = mapped_column(ForeignKey("db_servers.id"), index=True, nullable=False)
    db_name: Mapped[str] = mapped_column(String(64), nullable=False)
    db_user: Mapped[str] = mapped_column(String(32), nullable=False)
    # Fernet token (ciphertext) — the app db user's password, via app/crypto.
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(default=0, nullable=False)
    over_quota: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Per-user connection cap last applied to the DB server (ALTER USER / ALTER ROLE). NULL = unknown (new row or
    # credentials rotated): the next sweep applies the effective limit once, then records it here.
    db_conn_limit_applied: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)

    app: Mapped["App"] = relationship(back_populates="database")
    server: Mapped["DbServer"] = relationship(back_populates="databases")


class AppStorage(Base):
    """The one Spaces bucket + access key provisioned for an app that opted
    into `want_storage` (ARCHITECTURE.md §2 "Storage"). One-to-one with App.
    Kept consistent with AppDatabase: the secret (secret_key) is Fernet-
    encrypted at rest via app/crypto, everything else is plain."""

    __tablename__ = "app_storage"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"), unique=True, nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    access_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Fernet token (ciphertext) — the scoped key's secret, via app/crypto.
    secret_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(default=0, nullable=False)
    over_quota: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)

    app: Mapped["App"] = relationship(back_populates="storage")


class AppBackupConfig(Base):
    """Per-app backup settings and last-result summary (app/backups.py; docs/design/backups.md). Created lazily, only
    while BACKUPS_ENABLED. One restic repo per app and kind (db | vol) in the backup bucket, one random repo password
    per app (Fernet-encrypted here, never logged, passed to Jobs only through an ephemeral Secret)."""
    __tablename__ = "app_backup_config"

    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), primary_key=True, autoincrement=False)
    # Opt-in: new rows take config.BACKUP_DEFAULT_ENABLED (default off); existing rows keep their value.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    schedule: Mapped[str] = mapped_column(String(8), nullable=False, default="daily", server_default="daily")  # daily|weekly|off
    # Platform-admin lock: while set, scheduled and manual backups don't run and the owner can't turn them back on.
    # Existing backups are kept. `max_bytes_override` is an optional per-app storage ceiling (admin), else the platform default.
    admin_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    admin_disabled_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    max_bytes_override: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    repo_password_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_backup_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, index=True)
    last_backup_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)  # newest finished run of any kind
    last_success_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # succeeded | failed
    last_error: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Per kind (db = database dump; vol = volume, phase 1b): last finish time/status/error, bytes added by that run,
    # and the repo's total size after it (metered separately from the DB/storage quotas).
    db_last_backup_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    db_last_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    db_last_error: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    db_last_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    db_repo_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    vol_last_backup_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    vol_last_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    vol_last_error: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    vol_last_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    vol_repo_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    # Blob storage bucket kind ("bucket"): same fields as above.
    bucket_last_backup_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    bucket_last_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    bucket_last_error: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    bucket_last_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    bucket_repo_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    # Set when the app is deleted after its final backup: repos are purged once this passes (BACKUP_DELETED_RETENTION_DAYS).
    deleted_retention_until: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    purged_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)


class BackupRun(Base):
    """One backup (or purge) Job for one app and kind. queued -> running -> succeeded | failed. Only short, redacted
    summaries are stored: never restic output, repository URLs or credentials."""
    __tablename__ = "backup_runs"
    __table_args__ = (
        Index("ix_backup_runs_app_created", "app_id", "created_at", "id"),  # leading app_id also serves as the FK index
        Index("ix_backup_runs_status", "status"),  # the scheduler polls queued/running rows every tick
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # db | volume | bucket (| package: a set export)
    trigger: Mapped[str] = mapped_column(String(12), nullable=False)  # scheduled | manual | final | purge | restore | export | delete | prune
    # queued | running | succeeded | failed; a backup also passes through `deleting` and ends `deleted` (hidden); an export may be `expired`.
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    bytes_added: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # new data this run stored
    repo_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # repo size after the run
    snapshot_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # restic short snapshot id
    error: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    job_name: Mapped[Optional[str]] = mapped_column(String(63), nullable=True)
    # trigger == "restore": app_id is the TARGET app; this is the backup run (succeeded snapshot) being restored.
    # No FK: the source run/app may be long gone from the app's point of view (retained repo after deletion).
    source_run_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    # trigger == "export": app_id owns the source run (source_run_id); the finished export is this object in the backup
    # bucket (deleted ~1 hour after it is written). The bytes of the export are stored in bytes_added.
    export_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # The backup package (BackupSet) this run belongs to: a db/bucket/volume part of a backup (trigger scheduled|manual|final), the
    # per-part delete of a set, or the ONE export of a set (trigger == "export", kind "package"). NULL for purge/restore/prune and for
    # per-run exports.
    set_id: Mapped[Optional[int]] = mapped_column(ForeignKey("backup_sets.id", ondelete="SET NULL"), nullable=True, index=True)


class BackupSet(Base):
    """One backup as the owner sees it: everything one "Back up now" / scheduled slot / final backup produced (db, bucket and
    volume parts are separate BackupRuns with this set_id, taken one after another, not at one instant). Status is derived
    from the parts (app/backups.py::set_status); only the owner delete is stored, in `delete_status`."""
    __tablename__ = "backup_sets"
    __table_args__ = (Index("ix_backup_sets_app_created", "app_id", "created_at", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), nullable=False)
    trigger: Mapped[str] = mapped_column(String(12), nullable=False)  # scheduled | manual | final
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    delete_status: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # NULL | deleting | deleted
    # The app's image (as configured, e.g. ghcr.io/x/y:1.2) and pinned manifest digest when the backup was taken; for MANIFEST.json.
    image: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    image_digest: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)


class PlatformBackupRun(Base):
    """One backup Job of the platform's own database (app/platform_backup.py). No app: not tied to any tenant."""
    __tablename__ = "platform_backup_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(12), nullable=False)  # scheduled | manual
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued")  # queued | running | succeeded | failed
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    bytes_added: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    repo_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    snapshot_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    job_name: Mapped[Optional[str]] = mapped_column(String(63), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False, index=True)
    actor_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    actor_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Short, secret-free note. Never passwords, keys or env values.
    detail: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class AuthThrottle(Base):
    """Fixed-window counters for login/register throttling, shared by all replicas (app/ratelimit.py).
    `key` is a hash, so no email address or IP is stored in the clear."""

    __tablename__ = "auth_throttle"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(nullable=False, index=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


def release_unique_hosts(app: "App") -> None:
    """Free the unique host claims (platform_subdomain, custom_domain). Called by the reconciler once teardown
    has completed (NOT at soft-delete), so a host is never reassigned while its old Ingress still exists."""
    app.platform_subdomain = None
    app.custom_domain = None
    app.custom_domain_token = None
    app.custom_domain_status = None
    app.custom_domain_error = None
    app.custom_domain_verified_at = None


class PlatformSetting(Base):
    """Admin-set override of one platform setting (registry in app/platform_settings.py). No row = the default."""
    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


from app.kaisen import models as _kaisen_models  # noqa: E402,F401  (registers the kaisen_* tables on Base.metadata)
