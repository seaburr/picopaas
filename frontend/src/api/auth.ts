import type { User } from '../types'
import { apiFetch } from './client'

// Matches app/routers/auth.py exactly.
export const register = (email: string, password: string, acceptTerms: boolean) =>
  apiFetch<User>('/auth/register', {
    method: 'POST',
    body: JSON.stringify({ email, password, accept_terms: acceptTerms }),
  })

export const login = (email: string, password: string) =>
  apiFetch<User>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })

export const logout = () => apiFetch<void>('/auth/logout', { method: 'POST' })

export const getMe = () => apiFetch<User>('/auth/me')

// Backend: POST /auth/change-password (404 on older backends; callers degrade).
export const changePassword = (current_password: string, new_password: string) =>
  apiFetch<void>('/auth/change-password', {
    method: 'POST',
    body: JSON.stringify({ current_password, new_password }),
  })

// PATCH /auth/me: 422 "Unknown timezone" on bad input. null = browser default.
export const setTimezone = (timezone: string | null) =>
  apiFetch<User>('/auth/me', { method: 'PATCH', body: JSON.stringify({ timezone }) })
