import type { Project } from '../types'

// Project-scoped page URLs. The project id in the path is the only thing that decides which project a list, form or
// settings page shows; the stored "last clicked" project is only a best guess for the old unscoped URLs (router redirect).
export const projectApps = (pid: number) => `/projects/${pid}/apps`
export const projectNewApp = (pid: number) => `/projects/${pid}/apps/new`
export const projectKaisen = (pid: number, sub: '' | '/new' | '/settings' = '') => `/projects/${pid}/kaisen${sub}`
export const projectLabel = (p: Pick<Project, 'name' | 'is_personal'> | null | undefined) => (p ? (p.is_personal ? 'Personal' : p.name) : '')
