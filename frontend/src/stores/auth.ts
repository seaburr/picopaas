import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getMe, login as apiLogin, logout as apiLogout, register as apiRegister } from '../api/auth'
import type { User } from '../types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const loaded = ref(false)

  async function load() {
    try {
      user.value = await getMe()
    } catch {
      user.value = null
    } finally {
      loaded.value = true
    }
  }

  async function login(email: string, password: string) {
    user.value = await apiLogin(email, password)
    loaded.value = true
  }

  async function register(email: string, password: string, acceptTerms: boolean) {
    user.value = await apiRegister(email, password, acceptTerms)
    loaded.value = true
  }

  async function logout() {
    try {
      await apiLogout()
    } finally {
      user.value = null
    }
  }

  return { user, loaded, load, login, register, logout }
})
