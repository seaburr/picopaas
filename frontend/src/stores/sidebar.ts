import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getApp, listApps } from '../api/apps'
import type { App } from '../types'

// Data behind the sidebar tree. The API can list apps only per project (GET /apps?project_id=N; without project_id it
// returns every project's apps in one heavy call), so a project's apps are fetched lazily when its node opens and
// cached here. The CURRENT project is not polled: AppSidebar writes the apps store's list through (setFromStore).
export const useSidebarStore = defineStore('sidebar', () => {
  const byProject = ref<Record<number, App[]>>({})
  const errors = ref<Record<number, string>>({})
  const loading = ref<Record<number, boolean>>({})
  const stamps: Record<number, number> = {}

  async function loadProject(id: number, maxAgeMs = 0) {
    if (loading.value[id]) return
    if (maxAgeMs > 0 && stamps[id] && Date.now() - stamps[id] < maxAgeMs) return
    loading.value = { ...loading.value, [id]: true }
    try {
      byProject.value = { ...byProject.value, [id]: (await listApps(id)) ?? [] }
      stamps[id] = Date.now()
      const rest = { ...errors.value }
      delete rest[id]
      errors.value = rest
    } catch (e) {
      errors.value = { ...errors.value, [id]: e instanceof Error ? e.message : "Couldn't load apps." }
    } finally {
      loading.value = { ...loading.value, [id]: false }
    }
  }

  function setFromStore(id: number, apps: App[]) {
    byProject.value = { ...byProject.value, [id]: apps }
    stamps[id] = Date.now()
  }

  function find(appId: number): App | undefined {
    for (const list of Object.values(byProject.value)) {
      const hit = list.find((a) => a.id === appId)
      if (hit) return hit
    }
    return undefined
  }

  // The app behind the current /apps/:id route when no list has it yet (deep link): one GET, no polling.
  async function fetchOne(appId: number) {
    return getApp(appId)
  }

  function reset() {
    byProject.value = {}
    errors.value = {}
    loading.value = {}
    for (const k of Object.keys(stamps)) delete stamps[Number(k)]
  }

  return { byProject, errors, loading, loadProject, setFromStore, find, fetchOne, reset }
})
