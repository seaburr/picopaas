import type { App, AdminApp, AppResources, ResourcesPatch, ClusterHealth, AdminUser, AuditEntry, DbServer, DbServerCreate } from '../types'
import { apiFetch } from './client'

export const listUsers = () => apiFetch<AdminUser[]>('/admin/users')

export const setAppLimit = (id: number, app_limit: number) =>
  apiFetch<AdminUser>(`/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify({ app_limit }) })

export const listDbServers = () => apiFetch<DbServer[]>('/admin/db-servers')

export const createDbServer = (payload: DbServerCreate) =>
  apiFetch<DbServer>('/admin/db-servers', { method: 'POST', body: JSON.stringify(payload) })

export const patchDbServer = (id: number, payload: { status?: string; max_databases?: number }) =>
  apiFetch<DbServer>(`/admin/db-servers/${id}`, { method: 'PATCH', body: JSON.stringify(payload) })

const post = (path: string) => apiFetch<void>(path, { method: 'POST' })
export const approveUser = (id: number) => post(`/admin/users/${id}/approve`)
export const suspendUser = (id: number) => post(`/admin/users/${id}/suspend`)
export const unsuspendUser = (id: number) => post(`/admin/users/${id}/unsuspend`)
export const deleteUser = (id: number) => apiFetch<void>(`/admin/users/${id}`, { method: 'DELETE' })

export const listAllApps = () => apiFetch<AdminApp[]>('/admin/apps')
export const suspendApp = (id: number) => post(`/admin/apps/${id}/suspend`)
export const unsuspendApp = (id: number) => post(`/admin/apps/${id}/unsuspend`)
export const deleteAdminApp = (id: number) => apiFetch<void>(`/admin/apps/${id}`, { method: 'DELETE' })

export const listAudit = (limit = 100) => apiFetch<AuditEntry[]>(`/admin/audit?limit=${limit}`)

export const getHealth = () => apiFetch<ClusterHealth>('/admin/health')

export const patchAppResources = (id: number, body: ResourcesPatch) =>
  apiFetch<AppResources>(`/admin/apps/${id}/resources`, { method: 'PATCH', body: JSON.stringify(body) })

// Backend: POST /admin/users/{id}/reset-password -> { temporary_password } (shown once).
export const resetUserPassword = (id: number) =>
  apiFetch<{ temporary_password: string }>(`/admin/users/${id}/reset-password`, { method: 'POST' })

// Admin-only reserved <label>.<APP_DOMAIN> host. 422 bad/reserved label, 409 taken.
export const setPlatformHost = (id: number, subdomain: string) =>
  apiFetch<App>(`/admin/apps/${id}/platform-host`, { method: 'PUT', body: JSON.stringify({ subdomain }) })
export const clearPlatformHost = (id: number) =>
  apiFetch<App>(`/admin/apps/${id}/platform-host`, { method: 'DELETE' })
