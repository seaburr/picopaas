import type { App, AppCreatePayload, AppUpdatePayload, AppWithCreated } from '../types'
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
