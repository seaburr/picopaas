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

export const startBackupDownload = (id: number, runId: number) =>
  apiFetch<BackupExport>(`/apps/${id}/backups/${runId}/download`, { method: 'POST' })

export const getBackupExport = (id: number, exportId: number) =>
  apiFetch<BackupExport>(`/apps/${id}/backups/exports/${exportId}`)

export const listBackupExports = (id: number) => apiFetch<BackupExport[]>(`/apps/${id}/backups/exports`)

// 202 {run_id, status: "deleting"}; the run shows status "deleting" until it leaves runs[]. 409 busy (active export/job), 404 gone.
export const deleteBackupRun = (id: number, runId: number) =>
  apiFetch<{ run_id: number; status: string }>(`/apps/${id}/backups/${runId}`, { method: 'DELETE' })
