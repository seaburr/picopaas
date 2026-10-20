import { apiFetch } from './client'

// Matches app/routers/templates.py (GET /templates).
export interface AppTemplate {
  id: string
  name: string
  description: string
  image: string
  port: number
  wants_db: boolean
  wants_storage: boolean
  env: Record<string, string>
  notes: string
  docs_url?: string | null
}

export const listTemplates = () => apiFetch<AppTemplate[]>('/templates')
