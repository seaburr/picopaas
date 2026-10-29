import { defineStore } from 'pinia'
import { ref, watchEffect } from 'vue'
import { userZone } from '../utils/tz'
import { getMe, login as apiLogin, logout as apiLogout, register as apiRegister } from '../api/auth'
import { ApiError } from '../api/client'
import type { User } from '../types'
import { useProjectsStore } from './projects'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const loaded = ref(false)
  watchEffect(() => (userZone.value = user.value?.timezone ?? null))

  async function load() {
    try {
      user.value = (await getMe()) ?? null
    } catch {
      user.value = null
    } finally {
      loaded.value = true
    }
  }

  async function login(email: string, password: string) {
    user.value = await apiLogin(email, password)
    loaded.value = true
    await refresh() // /auth/me carries features + limits, which the login response may not
  }

  async function register(email: string, password: string, acceptTerms: boolean) {
    // The response may be the same "request received" for a duplicate email (no session cookie), so don't trust
    // it as proof of a session: ask /auth/me. Returns true when a session exists.
    await apiRegister(email, password, acceptTerms)
    await load()
    return user.value !== null
  }

  // Re-read /auth/me without clearing the user on a transient failure (used by the pending-page poll).
  async function refresh() {
    try {
      user.value = (await getMe()) ?? null
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) user.value = null
    }
  }

  async function logout() {
    try {
      await apiLogout()
    } finally {
      user.value = null
      useProjectsStore().reset()
    }
  }

  return { user, loaded, load, login, register, refresh, logout }
})
