import type { KaisenBuild, KaisenBuildDetail, KaisenFile, KaisenSettings, KaisenSettingsPayload, KaisenSpend, KaisenTestResult } from '../types'
import { apiFetch } from './client'

// Matches docs/design/kaisen-ui.md section 9 (app/kaisen routers).
const post = <T>(url: string, body?: unknown) =>
  apiFetch<T>(url, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const listBuilds = (projectId?: number | null) =>
  apiFetch<KaisenBuild[]>(projectId ? `/kaisen/builds?project_id=${projectId}` : '/kaisen/builds')
export const getBuild = (id: number) => apiFetch<KaisenBuildDetail>(`/kaisen/builds/${id}`)
export const createBuild = (payload: { project_id: number | null; name: string; brief: string }) =>
  post<KaisenBuildDetail>('/kaisen/builds', payload)
export const deleteBuild = (id: number, deleteApp: boolean) =>
  apiFetch<void>(`/kaisen/builds/${id}?delete_app=${deleteApp ? 'true' : 'false'}`, { method: 'DELETE' })
export const runBuild = (id: number) => post<KaisenBuildDetail>(`/kaisen/builds/${id}/run`)
export const sendFeedback = (id: number, text: string) => post<KaisenBuildDetail>(`/kaisen/builds/${id}/feedback`, { text })
export const answerInbox = (id: number, itemId: number, text: string) =>
  post<KaisenBuildDetail>(`/kaisen/builds/${id}/inbox/${itemId}/answer`, { text })
export const deployBuild = (id: number) => post<KaisenBuildDetail>(`/kaisen/builds/${id}/deploy`)
export const pushBuild = (id: number) => post<KaisenBuildDetail>(`/kaisen/builds/${id}/push`)
export const getBuildFile = (id: number, path: string) =>
  apiFetch<KaisenFile>(`/kaisen/builds/${id}/files/${path.split('/').map(encodeURIComponent).join('/')}`)
export const downloadUrl = (id: number) => `/kaisen/builds/${id}/download`

export const getKaisenSettings = (projectId: number) => apiFetch<KaisenSettings>(`/projects/${projectId}/kaisen`)
export const saveKaisenSettings = (projectId: number, payload: KaisenSettingsPayload) =>
  apiFetch<KaisenSettings>(`/projects/${projectId}/kaisen`, { method: 'PUT', body: JSON.stringify(payload) })
export const testKaisen = (projectId: number, which: 'anthropic' | 'github') =>
  post<KaisenTestResult>(`/projects/${projectId}/kaisen/test/${which}`)
export const getKaisenSpend = (projectId?: number | null) =>
  apiFetch<KaisenSpend>(projectId ? `/kaisen/spend?project_id=${projectId}` : '/kaisen/spend')
