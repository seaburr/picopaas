// Mirrors app/schemas.py exactly. Keep in sync with the backend by hand —
// there is no shared schema generation (yet).

export interface User {
  id: number
  email: string
  role: string
  app_limit: number
  status: UserStatus
  created_at: string
}

export type UserStatus = 'pending' | 'active' | 'suspended' | 'deleted'

export interface EnvVar {
  key: string
  value: string
  is_secret: boolean
}

export interface ApiKey {
  id: number
  prefix: string
  created_at: string
  revoked_at: string | null
}

// Returned only once, at creation time (app/schemas.py ApiKeyCreateOut) — the
// raw `key` is never stored or shown again after this response.
export interface ApiKeyCreated {
  id: number
  key: string
  prefix: string
  created_at: string
}

export interface App {
  id: number
  owner_id: number
  name: string
  hostname: string
  image: string
  port: number
  want_db: boolean
  want_storage: boolean
  status: string
  status_detail: string | null
  created_at: string
  health_check_enabled: boolean
  health_check_path: string
  env: EnvVar[]
}

export interface AppCreatePayload {
  name: string
  image: string
  port: number
  want_db: boolean
  want_storage: boolean
  health_check_enabled?: boolean
  health_check_path?: string
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
  app_count: number
  status: UserStatus
  created_at: string
}

export interface DbServer {
  id: number
  name: string
  host: string
  port: number
  status: string
  max_databases: number
  used: number
  pct: number
  warn: boolean
}

export interface DbServerCreate {
  name: string
  host: string
  port: number
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
  cpu_limit_m: number
  mem_limit_mi: number
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
}
