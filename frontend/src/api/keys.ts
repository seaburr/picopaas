import type { ApiKey, ApiKeyCreated } from '../types'
import { apiFetch } from './client'

// Matches app/routers/api_keys.py exactly (prefix "/api/keys").
export const listApiKeys = () => apiFetch<ApiKey[]>('/api/keys')

export const createApiKey = () => apiFetch<ApiKeyCreated>('/api/keys', { method: 'POST' })

export const revokeApiKey = (id: number) => apiFetch<void>(`/api/keys/${id}`, { method: 'DELETE' })
