import { defineStore } from 'pinia'
import { ref } from 'vue'
import { checkImage as checkImageApi, createApp, deleteApp, getApp, listApps, restartApp, updateApp } from '../api/apps'
import type { App, AppCreatePayload, AppUpdatePayload } from '../types'

export const useAppsStore = defineStore('apps', () => {
  const apps = ref<App[]>([])
  const loaded = ref(false)

  async function load() {
    apps.value = (await listApps()) ?? []
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

  function upsert(app: App) {
    apps.value = apps.value.some((a) => a.id === app.id)
      ? apps.value.map((a) => (a.id === app.id ? app : a))
      : [app, ...apps.value]
  }

  async function restart(id: number) {
    const app = await restartApp(id)
    if (app) upsert(app)
    return app
  }

  async function checkImage(id: number) {
    const app = await checkImageApi(id)
    if (app) upsert(app)
    return app
  }

  return { apps, loaded, load, fetchOne, create, update, remove, restart, checkImage, upsert }
})
