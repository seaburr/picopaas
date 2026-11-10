import { defineStore } from 'pinia'
import { ref } from 'vue'
import { checkImage as checkImageApi, createApp, deleteApp, getApp, getAppSwr, listApps, restartApp, rollbackApp, updateApp } from '../api/apps'
import { useProjectsStore } from './projects'
import type { App, AppCreatePayload, AppUpdatePayload } from '../types'

export const useAppsStore = defineStore('apps', () => {
  const apps = ref<App[]>([])
  const loaded = ref(false)
  const pid = ref<number | null>(null) // the project `apps` belongs to, so a return visit can show it at once

  // Latest request wins: a slow response for an earlier project must not overwrite a newer list.
  let seq = 0
  async function load(forPid?: number | null) {
    const mine = ++seq
    const target = forPid ?? useProjectsStore().current?.id ?? null
    const list = (await listApps(target ?? undefined)) ?? []
    if (mine !== seq) return
    apps.value = list
    pid.value = target
    loaded.value = true
  }

  // Drop the shown list (project switch) and invalidate any request still in flight.
  function clear() {
    seq++
    apps.value = []
    pid.value = null
  }

  async function fetchOne(id: number) {
    return getApp(id)
  }
  // First load of a page: a recent copy at once, `onFresh` gets the refreshed one. Polls and post-action reloads use fetchOne.
  async function fetchOneSwr(id: number, onFresh?: (a: App) => void) {
    return getAppSwr(id, onFresh)
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

  async function rollback(id: number, digest: string) {
    const app = await rollbackApp(id, digest)
    if (app) upsert(app)
    return app
  }

  async function checkImage(id: number) {
    const app = await checkImageApi(id)
    if (app) upsert(app)
    return app
  }

  function reset() {
    seq++
    apps.value = []
    pid.value = null
    loaded.value = false
  }

  return { apps, loaded, pid, reset, clear, load, fetchOne, fetchOneSwr, create, update, remove, restart, rollback, checkImage, upsert }
})
