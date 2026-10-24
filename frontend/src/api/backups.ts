import type { BackupExport, BackupRun, BackupsInfo } from '../types'
import { apiFetch } from './client'

// Matches app/routers/backups.py (prefix "/apps"). Errors: 409 busy, 429 limit (message has the wait), 422/503 unavailable.
export const getBackups = (id: number) => apiFetch<BackupsInfo>(`/apps/${id}/backups`)

export const setBackupConfig = (id: number, enabled: boolean, schedule?: string) =>
  apiFetch<BackupsInfo>(`/apps/${id}/backups`, {
    method: 'PATCH',
    body: JSON.stringify(schedule ? { enabled, schedule } : { enabled }),
  })

export const backUpNow = (id: number) => apiFetch<BackupRun[]>(`/apps/${id}/backups`, { method: 'POST' })

// 202 export (kind "package"); poll getBackupExport until ready.
export const downloadBackupSet = (id: number, setId: number) =>
  apiFetch<BackupExport>(`/apps/${id}/backups/sets/${setId}/download`, { method: 'POST' })

export const getBackupExport = (id: number, exportId: number) =>
  apiFetch<BackupExport>(`/apps/${id}/backups/exports/${exportId}`)

export const listBackupExports = (id: number) => apiFetch<BackupExport[]>(`/apps/${id}/backups/exports`)

// 202 {set_id, status, delete_run_ids}; the backup shows status "deleting" until it disappears. 409 busy, 404 gone.
export const deleteBackupSet = (id: number, setId: number) =>
  apiFetch<{ set_id: number; status: string; delete_run_ids: number[] }>(`/apps/${id}/backups/sets/${setId}`, { method: 'DELETE' })
