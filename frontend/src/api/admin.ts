import type { AdminUser, DbServer, DbServerCreate } from '../types'
import { apiFetch } from './client'

export const listUsers = () => apiFetch<AdminUser[]>('/admin/users')

export const setAppLimit = (id: number, app_limit: number) =>
  apiFetch<AdminUser>(`/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify({ app_limit }) })

export const listDbServers = () => apiFetch<DbServer[]>('/admin/db-servers')

export const createDbServer = (payload: DbServerCreate) =>
  apiFetch<DbServer>('/admin/db-servers', { method: 'POST', body: JSON.stringify(payload) })

export const patchDbServer = (id: number, payload: { status?: string; max_databases?: number }) =>
  apiFetch<DbServer>(`/admin/db-servers/${id}`, { method: 'PATCH', body: JSON.stringify(payload) })
