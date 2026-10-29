import type { App, AdminApp, ResourcesPatch, ResourcesPatchResult, ClusterHealth, AdminUser, AdminApiKey, AuditEntry, DbServer, DbServerCreate, SettingsGroup, BackupHealth, BackupsInfo, AdminBackupSets, BackupExport, PlatformPods, PlatformLogs } from '../types'
import { apiFetch, downloadFile } from './client'

export const listUsers = () => apiFetch<AdminUser[]>('/admin/users')

export const setAppLimit = (id: number, app_limit: number) =>
  apiFetch<AdminUser>(`/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify({ app_limit }) })

export const listDbServers = () => apiFetch<DbServer[]>('/admin/db-servers')

export const createDbServer = (payload: DbServerCreate) =>
  apiFetch<DbServer>('/admin/db-servers', { method: 'POST', body: JSON.stringify(payload) })

export const patchDbServer = (id: number, payload: { status?: string; max_databases?: number; admin_password?: string; ca_cert?: string }) =>
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
  apiFetch<ResourcesPatchResult>(`/admin/apps/${id}/resources`, { method: 'PATCH', body: JSON.stringify(body) })

// Backend: POST /admin/users/{id}/reset-password -> { temporary_password } (shown once).
export const resetUserPassword = (id: number) =>
  apiFetch<{ temporary_password: string }>(`/admin/users/${id}/reset-password`, { method: 'POST' })

// Admin-only reserved <label>.<APP_DOMAIN> host. 422 bad/reserved label, 409 taken.
export const setPlatformHost = (id: number, subdomain: string) =>
  apiFetch<App>(`/admin/apps/${id}/platform-host`, { method: 'PUT', body: JSON.stringify({ subdomain }) })
export const clearPlatformHost = (id: number) =>
  apiFetch<App>(`/admin/apps/${id}/platform-host`, { method: 'DELETE' })

export const setAppReplicas = (id: number, replicas: number) =>
  apiFetch<unknown>(`/admin/apps/${id}/replicas`, { method: 'PUT', body: JSON.stringify({ replicas }) })

export const getAdminGuide = () => apiFetch<{ markdown: string }>('/admin/docs/admin-guide')

export const promoteUser = (id: number) => apiFetch<AdminUser>(`/admin/users/${id}/promote`, { method: 'POST' })
export const demoteUser = (id: number) => apiFetch<AdminUser>(`/admin/users/${id}/demote`, { method: 'POST' })

export const downloadAuditLog = (format: 'csv' | 'json') =>
  downloadFile(`/admin/audit/export?format=${format}`, `audit-log.${format}`)

// ---- Admin settings: PUT body maps key -> value, or null to reset that key to its default. 422 detail is a string. ----
export const getSettings = () => apiFetch<{ groups: SettingsGroup[] }>('/admin/settings')
export const putSettings = (settings: Record<string, number | boolean | null>) =>
  apiFetch<{ groups: SettingsGroup[] }>('/admin/settings', { method: 'PUT', body: JSON.stringify({ settings }) })

// ---- Health extras ----
export const getBackupHealth = (days = 7) => apiFetch<BackupHealth>(`/admin/health/backups?days=${days}`)
export const adminBackupSets = (appId: number) => apiFetch<AdminBackupSets>(`/admin/apps/${appId}/backup-sets`)
export const adminDownloadBackupSet = (appId: number, setId: number) =>
  apiFetch<BackupExport>(`/admin/apps/${appId}/backup-sets/${setId}/download`, { method: 'POST' })
export const adminGetBackupExport = (appId: number, exportId: number) =>
  apiFetch<BackupExport>(`/admin/apps/${appId}/backup-exports/${exportId}`)
export const getPlatformPods = () => apiFetch<PlatformPods>('/admin/health/logs/pods')
export interface LogQuery { namespace: string; pod: string; container: string; tail: number; since_seconds?: number | null; previous: boolean }
const logQs = (q: LogQuery) => {
  const p = new URLSearchParams({ namespace: q.namespace, pod: q.pod, container: q.container, tail: String(q.tail) })
  if (q.since_seconds) p.set('since_seconds', String(q.since_seconds))
  if (q.previous) p.set('previous', 'true')
  return p.toString()
}
export const getPlatformLogs = (q: LogQuery) => apiFetch<PlatformLogs>(`/admin/health/logs?${logQs(q)}`)
export const downloadPlatformLogs = (q: LogQuery) =>
  downloadFile(`/admin/health/logs/download?${logQs(q)}`, `${q.namespace}_${q.pod}_${q.container}.log`)

// Per-app backup lock / storage ceiling. Send only what changes; max_bytes null clears the override.
export const runPlatformDbBackup = () =>
  apiFetch<{ run_id: number; status: string }>('/admin/health/backups/platform-db/run', { method: 'POST' })
export const getAppBackups = (id: number) => apiFetch<BackupsInfo>(`/admin/apps/${id}/backups`)
export const putAppBackups = (id: number, body: { admin_disabled?: boolean; reason?: string; max_bytes?: number | null }) =>
  apiFetch<BackupsInfo>(`/admin/apps/${id}/backups`, { method: 'PUT', body: JSON.stringify(body) })

export const listUserApiKeys = (userId: number) => apiFetch<AdminApiKey[]>(`/admin/users/${userId}/api-keys`)
export const revokeUserApiKey = (userId: number, keyId: number) =>
  apiFetch<void>(`/admin/users/${userId}/api-keys/${keyId}`, { method: 'DELETE' })

export const listAdminProjects = () => apiFetch<import('../types').AdminProject[]>('/admin/projects')

export const setProjectAppLimit = (id: number, app_limit: number | null) =>
  apiFetch<import('../types').AdminProject>(`/admin/projects/${id}`, { method: 'PATCH', body: JSON.stringify({ app_limit }) })
