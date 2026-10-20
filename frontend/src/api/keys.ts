import type { ApiKey, ApiKeyCreated } from '../types'
import { apiFetch } from './client'

// Matches app/routers/api_keys.py (prefix "/api/keys"). `name` is optional and ignored by older backends.
export const listApiKeys = () => apiFetch<ApiKey[]>('/api/keys')

export const createApiKey = (name?: string) =>
  apiFetch<ApiKeyCreated>('/api/keys', name ? { method: 'POST', body: JSON.stringify({ name }) } : { method: 'POST' })

export const revokeApiKey = (id: number) => apiFetch<void>(`/api/keys/${id}`, { method: 'DELETE' })
