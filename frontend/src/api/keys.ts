import type { ApiKey, ApiKeyCreated, ApiKeyScope } from '../types'
import { apiFetch } from './client'

// Matches app/routers/api_keys.py (prefix "/api/keys"). `name`/`scope` are optional; older backends ignore them
// (and treat every key as full access).
export const listApiKeys = () => apiFetch<ApiKey[]>('/api/keys')

export const createApiKey = (name?: string, scope?: ApiKeyScope) => {
  const body: Record<string, unknown> = {}
  if (name) body.name = name
  if (scope) body.scope = scope
  return apiFetch<ApiKeyCreated>('/api/keys', { method: 'POST', body: JSON.stringify(body) })
}

export const revokeApiKey = (id: number) => apiFetch<void>(`/api/keys/${id}`, { method: 'DELETE' })
