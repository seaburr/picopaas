import type { App, AppCreatePayload, AppUpdatePayload } from '../types'
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
