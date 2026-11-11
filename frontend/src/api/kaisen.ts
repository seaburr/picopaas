import type { KaisenDiff, KaisenRestoreResult, KaisenSnapshot, KaisenBuild, KaisenBuildSecret, KaisenMembers, KaisenSecretsResult, KaisenBuildDetail, KaisenFile, KaisenOutputPayload, KaisenSettings, KaisenSettingsPayload, KaisenSpend, KaisenTestResult } from '../types'
import { apiFetch } from './client'

// Matches docs/design/kaisen-ui.md section 9 (app/kaisen routers).
const post = <T>(url: string, body?: unknown) =>
  apiFetch<T>(url, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const listBuilds = (projectId?: number | null) =>
  apiFetch<KaisenBuild[]>(projectId ? `/kaisen/builds?project_id=${projectId}` : '/kaisen/builds')
export const getBuild = (id: number) => apiFetch<KaisenBuildDetail>(`/kaisen/builds/${id}`)
export const createBuild = (payload: { project_id: number | null; name: string; brief: string; output?: KaisenOutputPayload; start?: boolean }) =>
  post<KaisenBuildDetail>('/kaisen/builds', payload)
export const deleteBuild = (id: number, deleteApp: boolean) =>
  apiFetch<void>(`/kaisen/builds/${id}?delete_app=${deleteApp ? 'true' : 'false'}`, { method: 'DELETE' })
export const runBuild = (id: number) => post<KaisenBuildDetail>(`/kaisen/builds/${id}/run`)
export const interruptRun = (id: number, runId: number) => post<KaisenBuildDetail>(`/kaisen/builds/${id}/runs/${runId}/interrupt`)
export const sendFeedback = (id: number, text: string) => post<KaisenBuildDetail>(`/kaisen/builds/${id}/feedback`, { text })
export const answerInbox = (id: number, itemId: number, text: string) =>
  post<KaisenBuildDetail>(`/kaisen/builds/${id}/inbox/${itemId}/answer`, { text })
// Closes a notice for everyone in the project (questions are answered instead).
export const dismissInbox = (id: number, itemId: number) =>
  post<KaisenBuildDetail>(`/kaisen/builds/${id}/inbox/${itemId}/dismiss`)
export const deployBuild = (id: number) => post<KaisenBuildDetail>(`/kaisen/builds/${id}/deploy`)
export const pushBuild = (id: number) => post<import('../types').KaisenPushJob>(`/kaisen/builds/${id}/push`)
export const getPushJob = (id: number, pushId: number) => apiFetch<import('../types').KaisenPushJob>(`/kaisen/builds/${id}/push/${pushId}`)
export const getBuildFile = (id: number, path: string) =>
  apiFetch<KaisenFile>(`/kaisen/builds/${id}/files/${path.split('/').map(encodeURIComponent).join('/')}`)
export const saveBuildOutput = (id: number, payload: KaisenOutputPayload) =>
  apiFetch<KaisenBuildDetail['output']>(`/kaisen/builds/${id}/output`, { method: 'PUT', body: JSON.stringify(payload) })
export const testBuildOutput = (id: number) => post<KaisenTestResult>(`/kaisen/builds/${id}/output/test`)
export const downloadUrl = (id: number) => `/kaisen/builds/${id}/download`

export const improveBrief = (payload: { project_id: number | null; brief: string }) =>
  post<{ brief: string; model: string; usd: number }>('/kaisen/improve-brief', payload)

export const getKaisenSettings = (projectId: number) => apiFetch<KaisenSettings>(`/projects/${projectId}/kaisen`)
export const saveKaisenSettings = (projectId: number, payload: KaisenSettingsPayload) =>
  apiFetch<KaisenSettings>(`/projects/${projectId}/kaisen`, { method: 'PUT', body: JSON.stringify(payload) })
export const testKaisen = (projectId: number, which: 'anthropic') =>
  post<KaisenTestResult>(`/projects/${projectId}/kaisen/test/${which}`)
export const getKaisenSpend = (projectId?: number | null) =>
  apiFetch<KaisenSpend>(projectId ? `/kaisen/spend?project_id=${projectId}` : '/kaisen/spend')

export const getKaisenMembers = (projectId: number) => apiFetch<KaisenMembers>(`/projects/${projectId}/kaisen/members`)
// null clears a limit; omit a field to leave it unchanged.
export const saveKaisenMember = (projectId: number, userId: number, payload: { cap_day?: number | null; cap_build?: number | null }) =>
  apiFetch<unknown>(`/projects/${projectId}/kaisen/members/${userId}`, { method: 'PUT', body: JSON.stringify(payload) })

export const listBuildSecrets = (id: number) => apiFetch<KaisenBuildSecret[]>(`/kaisen/builds/${id}/secrets`)
export const putBuildSecret = (id: number, name: string, value: string) =>
  apiFetch<KaisenSecretsResult>(`/kaisen/builds/${id}/secrets/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify({ value }) })
export const deleteBuildSecret = (id: number, name: string) =>
  apiFetch<KaisenSecretsResult>(`/kaisen/builds/${id}/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' })

export const listSnapshots = (id: number) => apiFetch<KaisenSnapshot[]>(`/kaisen/builds/${id}/snapshots`)
export const getSnapshotDiff = (id: number, sid: number) => apiFetch<KaisenDiff>(`/kaisen/builds/${id}/snapshots/${sid}/diff`)
export const getRunDiff = (id: number, runId: number) => apiFetch<KaisenDiff>(`/kaisen/builds/${id}/runs/${runId}/diff`)
export const restoreSnapshot = (id: number, sid: number) => post<KaisenRestoreResult>(`/kaisen/builds/${id}/snapshots/${sid}/restore`)

// ---- file uploads (raw bodies, not multipart) ----
const q = (o: Record<string, string>) => new URLSearchParams(o).toString()
export const listUploads = (id: number) => apiFetch<import('../types').KaisenUploadList>(`/kaisen/builds/${id}/uploads`)
export const addUpload = (id: number, file: File, kind: 'asset' | 'reference', description: string) =>
  apiFetch<import('../types').KaisenUploadResult>(`/kaisen/builds/${id}/uploads?${q({ kind, filename: file.name, description })}`, {
    method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' },
  })
export const replaceUpload = (id: number, uid: number, file: File, description?: string) =>
  apiFetch<import('../types').KaisenUploadResult>(`/kaisen/builds/${id}/uploads/${uid}?${q({ filename: file.name, ...(description === undefined ? {} : { description }) })}`, {
    method: 'PUT', body: file, headers: { 'Content-Type': 'application/octet-stream' },
  })
export const describeUpload = (id: number, uid: number, description: string) =>
  apiFetch<import('../types').KaisenUpload>(`/kaisen/builds/${id}/uploads/${uid}`, { method: 'PATCH', body: JSON.stringify({ description }) })
export const deleteUpload = (id: number, uid: number) => apiFetch<void>(`/kaisen/builds/${id}/uploads/${uid}`, { method: 'DELETE' })
export const uploadContentUrl = (id: number, uid: number, version: number) => `/kaisen/builds/${id}/uploads/${uid}/content?v=${version}`
