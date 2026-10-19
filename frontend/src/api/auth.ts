import type { User } from '../types'
import { apiFetch } from './client'

// Matches app/routers/auth.py exactly.
export const register = (email: string, password: string) =>
  apiFetch<User>('/auth/register', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })

export const login = (email: string, password: string) =>
  apiFetch<User>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })

export const logout = () => apiFetch<void>('/auth/logout', { method: 'POST' })

export const getMe = () => apiFetch<User>('/auth/me')
