import { defineStore } from 'pinia'
import { ref } from 'vue'
import { createApp, deleteApp, getApp, listApps, updateApp } from '../api/apps'
import type { App, AppCreatePayload, AppUpdatePayload } from '../types'

export const useAppsStore = defineStore('apps', () => {
  const apps = ref<App[]>([])
  const loaded = ref(false)

  async function load() {
    apps.value = await listApps()
    loaded.value = true
  }

  async function fetchOne(id: number) {
    return getApp(id)
  }

  async function create(payload: AppCreatePayload) {
    const app = await createApp(payload)
    apps.value = [app, ...apps.value]
    return app
  }

  async function update(id: number, payload: AppUpdatePayload) {
    const app = await updateApp(id, payload)
    apps.value = apps.value.map((a) => (a.id === id ? app : a))
    return app
  }

  async function remove(id: number) {
    await deleteApp(id)
    apps.value = apps.value.filter((a) => a.id !== id)
  }

  return { apps, loaded, load, fetchOne, create, update, remove }
})
