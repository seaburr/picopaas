import type { App, Project, ProjectInvite, ProjectInviteCreated, ProjectMember, ProjectRole } from '../types'
import { apiFetch } from './client'

// Matches app/routers/projects.py.
const json = (method: string, body: unknown) => ({ method, body: JSON.stringify(body) })

export const listProjects = () => apiFetch<Project[]>('/projects')
export const createProject = (name: string) => apiFetch<Project>('/projects', json('POST', { name }))
export const getProject = (id: number) => apiFetch<Project>(`/projects/${id}`)
export const renameProject = (id: number, name: string) => apiFetch<Project>(`/projects/${id}`, json('PATCH', { name }))
export const deleteProject = (id: number) => apiFetch<void>(`/projects/${id}`, { method: 'DELETE' })

export const listMembers = (id: number) => apiFetch<ProjectMember[]>(`/projects/${id}/members`)
export const setMemberRole = (id: number, userId: number, role: ProjectRole) =>
  apiFetch<ProjectMember>(`/projects/${id}/members/${userId}`, json('PATCH', { role }))
export const removeMember = (id: number, userId: number) => apiFetch<void>(`/projects/${id}/members/${userId}`, { method: 'DELETE' })
export const leaveProject = (id: number) => apiFetch<void>(`/projects/${id}/leave`, { method: 'POST' })

export const listInvites = (id: number) => apiFetch<ProjectInvite[]>(`/projects/${id}/invites`)
export const createInvite = (id: number, body: { email?: string; role?: ProjectRole }) =>
  apiFetch<ProjectInviteCreated>(`/projects/${id}/invites`, json('POST', body))
export const revokeInvite = (id: number, inviteId: number) => apiFetch<void>(`/projects/${id}/invites/${inviteId}`, { method: 'DELETE' })
export const acceptInvite = (token: string) => apiFetch<Project>(`/invites/${encodeURIComponent(token)}/accept`, { method: 'POST' })

export const moveApp = (appId: number, projectId: number) => apiFetch<App>(`/apps/${appId}/move`, json('POST', { project_id: projectId }))
