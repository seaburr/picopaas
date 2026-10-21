import type { App, AppEvent, MetricsHistory, ImageVersion, AppCreatePayload, AppUpdatePayload, AppWithCreated } from '../types'
import { apiFetch } from './client'

// Matches app/routers/apps.py exactly (prefix "/apps").
export const listApps = () => apiFetch<App[]>('/apps')

export const getApp = (id: number) => apiFetch<App>(`/apps/${id}`)

export const createApp = (payload: AppCreatePayload) =>
  apiFetch<App>('/apps', {
    method: 'POST',
    body: JSON.stringify(payload),
  })

export const updateApp = (id: number, payload: AppUpdatePayload) =>
  apiFetch<App>(`/apps/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })

export const deleteApp = (id: number) => apiFetch<void>(`/apps/${id}`, { method: 'DELETE' })

export const applySpec = (spec: string, secrets: Record<string, string>) =>
  apiFetch<AppWithCreated>('/apps/spec', {
    method: 'POST',
    body: JSON.stringify({ spec, secrets }),
  })

export const rotateCredentials = (id: number, target: 'db' | 'storage') =>
  apiFetch<App>(`/apps/${id}/rotate-credentials`, {
    method: 'POST',
    body: JSON.stringify({ target }),
  })

export interface AppLogs {
  lines: string[]
  pod: string | null
  note: string | null
}

// Backend contract (docs/UX plan): GET /apps/{id}/logs?tail=N and POST /apps/{id}/restart. 404 = older backend.
export const getAppLogs = (id: number, tail = 200) => apiFetch<AppLogs>(`/apps/${id}/logs?tail=${tail}`)

export const getMetricsHistory = (id: number) => apiFetch<MetricsHistory>(`/apps/${id}/metrics-history`)

export const getAppEvents = (id: number) => apiFetch<AppEvent[]>(`/apps/${id}/events`)

export const checkImage = (id: number) => apiFetch<App>(`/apps/${id}/check-image`, { method: 'POST' })

export const restartApp = (id: number) => apiFetch<App>(`/apps/${id}/restart`, { method: 'POST' })

export interface StorageDeleteResult {
  deleted: number
  size_bytes: number
  truncated: boolean
  over_quota: boolean
}

// Deletes with the platform's credentials (an over-cap app only holds a read-only key).
export const deleteStorageObjects = (id: number, body: { prefix: string } | { all: true }) =>
  apiFetch<StorageDeleteResult>(`/apps/${id}/storage/delete`, { method: 'POST', body: JSON.stringify(body) })

export const getImageHistory = (id: number) => apiFetch<ImageVersion[]>(`/apps/${id}/image-history`)

export const rollbackApp = (id: number, digest: string) =>
  apiFetch<App>(`/apps/${id}/rollback`, { method: 'POST', body: JSON.stringify({ digest }) })

export const setCustomDomain = (id: number, host: string) =>
  apiFetch<App>(`/apps/${id}/domain`, { method: 'PUT', body: JSON.stringify({ host }) })

export const verifyCustomDomain = (id: number) =>
  apiFetch<App>(`/apps/${id}/domain/verify`, { method: 'POST' })

export const clearCustomDomain = (id: number) =>
  apiFetch<App>(`/apps/${id}/domain`, { method: 'DELETE' })
