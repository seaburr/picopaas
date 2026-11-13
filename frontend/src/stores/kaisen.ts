import { defineStore } from 'pinia'
import { ref } from 'vue'
import { deleteBuild, getKaisenSettings, getKaisenSpend, listBuilds } from '../api/kaisen'
import type { KaisenBuild, KaisenSettings, KaisenSpend } from '../types'

export const useKaisenStore = defineStore('kaisen', () => {
  const builds = ref<KaisenBuild[]>([])
  const settings = ref<KaisenSettings | null>(null)
  const spend = ref<KaisenSpend | null>(null)

  // Latest request wins: a slow response for an earlier project must not overwrite a newer list.
  let seq = 0
  const pid = ref<number | null>(null) // the project `builds` belongs to, so a return visit can show it at once
  function clear() {
    seq++
    builds.value = []
    settings.value = null
    spend.value = null
    pid.value = null
  }

  async function load(forPid: number) {
    const mine = ++seq
    const list = (await listBuilds(forPid)) ?? []
    if (mine !== seq) return
    builds.value = list
    pid.value = forPid
    // Spend and settings are secondary: a failure must not hide the list.
    const [sp, st] = await Promise.all([
      getKaisenSpend(forPid).catch(() => null),
      forPid ? getKaisenSettings(forPid).catch(() => null) : Promise.resolve(undefined),
    ])
    if (mine !== seq) return
    spend.value = sp
    if (st !== undefined) settings.value = st
  }

  // Quiet refresh for polling: builds only.
  async function refreshBuilds(pid: number) {
    const mine = seq
    const list = (await listBuilds(pid)) ?? []
    if (mine !== seq) return
    builds.value = list
  }

  function upsert(b: KaisenBuild) {
    builds.value = builds.value.some((x) => x.id === b.id) ? builds.value.map((x) => (x.id === b.id ? { ...x, ...b } : x)) : [b, ...builds.value]
  }

  async function remove(id: number, deleteApp: boolean) {
    await deleteBuild(id, deleteApp)
    builds.value = builds.value.filter((b) => b.id !== id)
  }

  return { builds, settings, spend, pid, clear, load, refreshBuilds, upsert, remove }
})
