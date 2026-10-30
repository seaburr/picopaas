import { defineStore } from 'pinia'
import { ref } from 'vue'
import { deleteBuild, getKaisenSettings, getKaisenSpend, listBuilds } from '../api/kaisen'
import { useProjectsStore } from './projects'
import type { KaisenBuild, KaisenSettings, KaisenSpend } from '../types'

export const useKaisenStore = defineStore('kaisen', () => {
  const builds = ref<KaisenBuild[]>([])
  const settings = ref<KaisenSettings | null>(null)
  const spend = ref<KaisenSpend | null>(null)

  async function load() {
    const pid = useProjectsStore().current?.id
    builds.value = (await listBuilds(pid)) ?? []
    // Spend and settings are secondary: a failure must not hide the list.
    try {
      spend.value = await getKaisenSpend(pid)
    } catch {
      spend.value = null
    }
    if (pid) {
      try {
        settings.value = await getKaisenSettings(pid)
      } catch {
        settings.value = null
      }
    }
  }

  // Quiet refresh for polling: builds only.
  async function refreshBuilds() {
    builds.value = (await listBuilds(useProjectsStore().current?.id)) ?? []
  }

  function upsert(b: KaisenBuild) {
    builds.value = builds.value.some((x) => x.id === b.id) ? builds.value.map((x) => (x.id === b.id ? { ...x, ...b } : x)) : [b, ...builds.value]
  }

  async function remove(id: number, deleteApp: boolean) {
    await deleteBuild(id, deleteApp)
    builds.value = builds.value.filter((b) => b.id !== id)
  }

  return { builds, settings, spend, load, refreshBuilds, upsert, remove }
})
