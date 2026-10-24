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
  db_engine: 'mysql' | 'pg'
  wants_storage: boolean
  env: Record<string, string>
  secrets: TemplateSecret[]
  health_check: { path: string; timeout_seconds: number | null } | null
  volume: { size_gi: number; mount_path: string; required: boolean } | null
  notes: string
  docs_url?: string | null
}

export const listTemplates = () => apiFetch<AppTemplate[]>('/templates')
