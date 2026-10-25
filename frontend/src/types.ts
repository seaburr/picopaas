// Mirrors app/schemas.py exactly. Keep in sync with the backend by hand —
// there is no shared schema generation (yet).

export interface User {
  id: number
  email: string
  role: string
  app_limit: number
  max_apps?: number // GET /auth/me: hard per-account ceiling (applies to admins too)
  app_limit_effective?: number // GET /auth/me: the limit actually enforced
  status: UserStatus
  created_at: string
  timezone?: string | null // IANA name; null/absent = browser zone
  must_change_password?: boolean // true after an admin password reset
  // GET /auth/me only (optional until the backend ships them; treat undefined as "unknown, allow").
  features?: Features
  limits?: Limits
}

export interface Features {
  databases: boolean
  storage: boolean
  db_engines?: string[] // engines with free capacity now: a subset of ['mysql', 'pg']
  volumes?: boolean // persistent volumes enabled (off by default)
}

export interface Limits {
  cpu_limit_m: number
  mem_limit_mi: number
  cpu_request_m: number
  mem_request_mi: number
  image_mb: number
  db_mb: number
  bucket_mb: number
  ephemeral_mi?: number
  volume_max_gi?: number
  volume_default_gi?: number
  volume_override_max_gi?: number
  volume_default_mount_path?: string
  override: { cpu_min_m: number; cpu_max_m: number; mem_min_mi: number; mem_max_mi: number }
}

export interface AppVolume {
  size_gi: number
  mount_path: string
  phase: 'Pending' | 'Bound' | null
}

export type UserStatus = 'pending' | 'active' | 'suspended' | 'deleted'

export interface EnvVar {
  key: string
  value: string
  is_secret: boolean
}

export type ApiKeyScope = 'read' | 'full'

export interface ApiKey {
  id: number
  prefix: string
  created_at: string
  revoked_at: string | null
  name?: string | null
  scope?: ApiKeyScope
  last_used_at?: string | null
}

// Returned only once, at creation time (app/schemas.py ApiKeyCreateOut) — the
// raw `key` is never stored or shown again after this response.
export interface ApiKeyCreated {
  id: number
  key: string
  prefix: string
  created_at: string
  name?: string | null
  scope?: ApiKeyScope
}

export interface UsageStat {
  size_bytes: number
  limit_bytes: number
  over_quota: boolean
}

export interface AppQuota {
  db: UsageStat | null
  storage: UsageStat | null
  volume?: UsageStat | null
}

export interface AppEvent {
  at: string
  kind: 'disk' | 'evicted' | 'oom' | 'crashed' | 'rollback' | string
  message: string
}

export interface MetricsHistory {
  points: { ts: string; cpu_m: number; mem_mi: number; pods?: number; per_pod?: Record<string, { cpu_m: number; mem_mi: number }> }[]
  cpu_limit_m: number // PER POD
  mem_limit_mi: number // PER POD
  replicas?: number
  pods?: number
  cpu_capacity_m?: number // max(replicas, pods) x per-pod limit
  mem_capacity_mi?: number
}

export interface ImageVersion {
  digest: string
  image_ref: string
  created_at: string
  current: boolean
}

export interface App {
  id: number
  owner_id: number
  name: string
  hostname: string
  platform_subdomain: string | null
  platform_host: string | null
  image: string
  image_digest?: string | null
  auto_update?: boolean
  image_checked_at?: string | null
  image_update_error?: string | null
  port: number
  want_db: boolean
  db_engine?: 'mysql' | 'pg'
  want_storage: boolean
  status: string
  status_detail: string | null
  // short machine code: image_pull | crash_loop | oom | not_ready | quota | root_image | unknown
  status_reason?: string | null
  created_at: string
  health_check_enabled: boolean
  health_check_path: string
  health_check_timeout_seconds?: number | null
  env: EnvVar[]
  retry_at?: string | null
  last_event?: AppEvent | null // newest restart event, only if under 24h old
  resources?: AppResources
  quota?: AppQuota
  custom_domain?: CustomDomain | null
  volume?: AppVolume | null
}

export interface CustomDomain {
  host: string
  status: 'pending' | 'active' | 'error'
  verified_at?: string | null
  error?: string | null
  challenge_host: string
  challenge_token: string
  cname_target: string
}

export interface ResourceValues {
  cpu_request_m: number
  cpu_limit_m: number
  mem_request_mi: number
  mem_limit_mi: number
  ephemeral_limit_mi: number
  db_max_connections: number
  db_quota_mb: number
  storage_quota_mb: number
  image_size_mb: number
  egress_mbit: number
}

// Effective values (override or platform default) plus the platform defaults for this app's engine.
export interface AppResources extends ResourceValues {
  overridden: boolean
  exceeds_platform_caps: boolean
  defaults: ResourceValues
}

export interface ResourcesPatch {
  cpu_request_m?: number | null
  cpu_limit_m?: number | null
  mem_request_mi?: number | null
  mem_limit_mi?: number | null
  // null = platform default
  db_max_connections?: number | null
  ephemeral_limit_mi?: number | null
  db_quota_mb?: number | null
  storage_quota_mb?: number | null
  image_size_mb?: number | null
  egress_mbit?: number | null
  volume_size_gi?: number // grow-only, 1..admin ceiling, app must have a volume
}

// PATCH /admin/apps/{id}/resources returns the updated AdminApp.
export interface ResourcesPatchResult extends AdminApp {
  restarts_app: boolean
  exceeds_platform_caps: boolean
}

export interface AppCreatePayload {
  name: string
  image: string
  port: number
  want_db: boolean
  db_engine?: 'mysql' | 'pg'
  want_storage: boolean
  health_check_enabled?: boolean
  health_check_path?: string
  health_check_timeout_seconds?: number | null
  auto_update?: boolean
  volume?: { size_gi?: number; mount_path?: string }
  env: EnvVar[]
}

// AppUpdate has no want_db/want_storage: the backend schema doesn't allow
// changing those after creation (app/schemas.py AppUpdate).
export interface AppUpdatePayload {
  name?: string
  image?: string
  port?: number
  health_check_enabled?: boolean
  health_check_path?: string
  health_check_timeout_seconds?: number | null
  auto_update?: boolean
  volume?: { size_gi?: number; mount_path?: string }
  env?: EnvVar[]
}

// app/schemas.py MASKED_VALUE — what the API returns in place of a secret's
// real value. Used by the env var editor to detect "unchanged" secrets.
export const MASKED_VALUE = '********'

// docs/ARCHITECTURE.md "Reference variables" — kept in sync with
// app/env_refs.py ALLOWED_REFS by hand.
export const DB_REFS = ['db.host', 'db.port', 'db.name', 'db.user', 'db.password', 'db.url', 'db.ca_cert', 'db.tls']
export const STORAGE_REFS = [
  'storage.endpoint',
  'storage.region',
  'storage.bucket',
  'storage.access_key',
  'storage.secret_key',
]

export interface AdminUser {
  id: number
  email: string
  role: string
  app_limit: number
  app_limit_effective: number
  app_count: number
  api_key_count?: number // active (unrevoked) keys
  status: UserStatus
  created_at: string
}

export interface DbServer {
  id: number
  name: string
  host: string
  port: number
  status: string
  engine: 'mysql' | 'pg'
  max_databases: number
  used: number
  pct: number
  warn: boolean
  // Live SQL metrics (admin health only); null when the server was unreachable.
  connections?: { used: number; running: number; max: number; max_used: number } | null
  data_size_bytes?: number | null
  uptime_seconds?: number | null
  error?: string | null
  disk_mb?: number | null
  // DO managed-DB host metrics; null when unavailable (host_metrics_error says why, null if simply not configured).
  host_metrics?: { cpu_pct: number | null; mem_pct: number | null; disk_pct: number | null } | null
  host_metrics_error?: string | null
}

export interface DbServerCreate {
  name: string
  host: string
  port: number
  engine: 'mysql' | 'pg'
  admin_user: string
  admin_password: string
  max_databases: number
}

export type AppWithCreated = App & { created: boolean }

export interface AdminApp {
  id: number
  name: string
  hostname: string
  status: string
  owner_id: number
  owner_email: string
  image: string
  created_at: string
  replicas: number
  want_db: boolean
  want_storage: boolean
  db_engine: 'mysql' | 'pg' | null
  platform_host: string | null
  volume?: AppVolume | null
  custom_domain: string | null // host only (not the CustomDomain object the app endpoints return)
  resources: AppResources
}

export interface AuditEntry {
  id: number
  at: string
  actor_email: string | null
  action: string
  target_type: string
  target_id: string | number | null
  detail: string | null
}

export interface HealthNode {
  name: string
  ready: boolean
  age_s: number
  cpu_capacity_m: number
  cpu_allocatable_m: number
  cpu_requested_m: number
  cpu_used_m: number | null
  mem_capacity_mi: number
  mem_allocatable_mi: number
  mem_requested_mi: number
  mem_used_mi: number | null
  pods: number
  pods_capacity: number
  disk_capacity_mi?: number | null
  disk_allocatable_mi?: number | null
  conditions?: Record<string, boolean>
}

export interface HealthApp {
  id: number
  name: string
  owner_email: string
  status: string
  namespace: string
  pods_ready: number
  pods_total: number
  restarts: number
  cpu_used_m: number | null
  mem_used_mi: number | null
  cpu_limit_m: number // per pod
  mem_limit_mi: number // per pod
  replicas?: number
  cpu_capacity_m?: number // aggregate; the denominator for cpu_used_m
  mem_capacity_mi?: number
  pod_usage?: HealthPodUsage[]
}

export interface HealthPodUsage {
  name: string
  ready: boolean
  restarts: number
  cpu_used_m: number | null
  mem_used_mi: number | null
}

export interface HealthPlatform {
  name: string
  namespace: string
  ready: boolean
  restarts: number
  cpu_used_m: number | null
  mem_used_mi: number | null
}

export interface ClusterHealth {
  generated_at: string
  metrics_available: boolean
  errors: string[]
  cluster: { node_count: number; max_nodes: number | null; pods_total: number }
  nodes: HealthNode[]
  apps: HealthApp[]
  platform: HealthPlatform[]
  db_servers: DbServer[]
  users?: { total: number; active: number; pending: number; suspended: number; admins?: number }
  pending_users?: { id: number; email: string; created_at: string }[]
  workloads?: {
    apps_total: number
    by_status: Partial<Record<'running' | 'deploying' | 'pending' | 'error' | 'suspended', number>>
    with_db: number
    with_storage: number
    with_volume?: number
    by_db_engine?: { mysql: number; postgres: number }
  }
}

// Backups (app/routers/backups.py). Datetimes are naive UTC strings; use parseTs().
export type BackupKind = 'db' | 'volume' | 'bucket'
export interface BackupRun {
  id: number
  kind: BackupKind | string
  trigger: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'deleting' | string
  created_at: string
  started_at: string | null
  finished_at: string | null
  bytes_added: number | null
  repo_bytes: number | null
  snapshot_id: string | null
  error: string | null
  source_run_id: number | null
  // Absent before the async-export backend deploys: treat as null.
  export?: BackupRunExport | null
  failed_export?: BackupFailedExport | null
}
export interface BackupFailedExport { id: number; error: string | null; at: string | null }
export interface BackupRunExport {
  id: number
  status: 'queued' | 'running' | 'ready' | 'failed' | 'expired'
  available_until: string | null
  error?: string | null
}
export interface BackupSetPart {
  run_id: number
  kind: BackupKind | string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'deleting' | string
  bytes_added: number | null
  error: string | null
}
export interface BackupSet {
  id: number
  trigger: 'scheduled' | 'manual' | 'final' | string
  created_at: string
  finished_at: string | null
  status: 'running' | 'succeeded' | 'partial' | 'failed' | 'deleting' | string
  size_bytes: number
  parts: BackupSetPart[]
  export: BackupRunExport | null
  failed_export?: BackupFailedExport | null
}
export interface VolumeBackups {
  has_volume: boolean
  available: boolean
  note: string | null
}
export interface BackupKindStatus {
  kind: BackupKind | string
  last_backup_at: string | null
  last_status: string | null
  last_error: string | null
  last_bytes: number | null
  repo_bytes: number
}
export interface BackupsInfo {
  feature_enabled: boolean
  configured: boolean
  enabled: boolean
  schedule: 'daily' | 'weekly' | 'off' | string
  state: string
  kinds: BackupKindStatus[]
  // Absent on an older backend: the card falls back to a message.
  backups?: BackupSet[]
  volume_backups?: VolumeBackups
  runs: BackupRun[]
  next_backup_at: string | null
  last_backup_at: string | null
  last_success_at: string | null
  cap_bytes: number
  repo_bytes?: number
  admin_disabled?: boolean
  admin_disabled_reason?: string | null
  admin_disabled_message?: string | null
  max_held?: number
  held?: number
  manual_per_hour?: number
  export_per_hour?: number
  storage_full?: boolean
  cap_override?: boolean
}
export interface BackupExport {
  export_id: number
  run_id: number
  kind: string
  status: 'queued' | 'running' | 'ready' | 'failed' | 'expired'
  filename: string | null
  size_bytes: number | null
  url: string | null
  url_expires_in: number | null
  available_until: string | null
  error: string | null
  created_at: string
}

// ---- Admin settings (GET/PUT /admin/settings) ----
export interface PlatformSetting {
  key: string
  label: string
  description: string
  type: 'int' | 'bool'
  unit: string
  value: number | boolean
  default: number | boolean
  overridden: boolean
  ceiling_key?: string | null
  min: number | null
  max: number | null
  updated_at: string | null
  updated_by: string | null
}
export interface SettingsGroup { name: string; settings: PlatformSetting[] }

// ---- Admin health extras (GET /admin/health/backups, /admin/health/logs/pods) ----
export interface BackupTopApp {
  app_id: number; name: string; owner_email: string | null; deleted: boolean; purge_after?: string | null
  repo_bytes: number; db_bytes: number; volume_bytes: number; bucket_bytes: number
  cap_bytes?: number; full?: boolean; admin_disabled?: boolean
}
export interface BackupStorage {
  used_bytes: number; cap_bytes: number; used_pct: number | null; apps_with_backups: number
  top_apps: BackupTopApp[]; over_cap?: boolean; warning: string | null; note: string
}
export interface BackupFailure {
  run_id: number; app_id: number; app_name: string | null; app_deleted: boolean; owner_email: string | null
  kind: string; trigger: string; status: string; error: string | null; at: string | null; still_failing?: boolean; age_seconds?: number
}
export interface BackupHealth {
  enabled: boolean; configured: boolean; days: number
  storage: BackupStorage; failures: BackupFailure[]; stuck: BackupFailure[]
  sweeper?: { last_run_at: string | null; cleaned: Record<string, number>; errors: string[]; orphan_repos: (string | number)[] }
}
export interface PlatformPod { namespace: string; name: string; phase?: string; ready?: boolean; restarts?: number; containers: string[] }
export interface PlatformPods { namespaces: string[]; pods: PlatformPod[] }
export interface PlatformLogs { namespace: string; pod: string; container: string; previous: boolean; lines: string[]; truncated: boolean }

// GET /admin/users/{id}/api-keys (never includes the secret or hash).
export interface AdminApiKey {
  id: number
  prefix: string
  name: string | null
  scope: ApiKeyScope
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}
