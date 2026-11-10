import { apiFetch } from './client'

// Matches app/routers/templates.py (GET /templates).
export interface TemplateSecret {
  key: string
  hint: string
}

export interface AppTemplate {
  id: string
  name: string
  description: string
  image: string
  port: number
  wants_db: boolean
  available?: boolean // false: needs a database engine with no server attached (e.g. PostgreSQL torn down)
  db_engine: 'mysql' | 'pg'
  wants_storage: boolean
  allow_sensitive_paths?: boolean
  env: Record<string, string>
  secrets: TemplateSecret[]
  health_check: { path: string; timeout_seconds: number | null } | null
  volume: { size_gi: number; mount_path: string; required: boolean } | null
  notes: string
  resources?: Partial<Record<'cpu_limit_m' | 'mem_limit_mi' | 'ephemeral_limit_mi' | 'storage_quota_mb', number>> | null
  docs_url?: string | null
  upstream_docs_url?: string | null
}

export const listTemplates = () => apiFetch<AppTemplate[]>('/templates')
