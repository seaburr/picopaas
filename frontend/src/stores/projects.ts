import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { createProject, listProjects } from '../api/projects'
import type { Project } from '../types'

const KEY = 'picopaas.project'
function readStored(): number | null {
  try {
    const n = Number(localStorage.getItem(KEY))
    return Number.isInteger(n) && n > 0 ? n : null
  } catch {
    return null
  }
}

export const useProjectsStore = defineStore('projects', () => {
  const projects = ref<Project[]>([])
  const loaded = ref(false)
  const loadError = ref('')
  const currentId = ref<number | null>(readStored())

  const personal = computed(() => projects.value.find((p) => p.is_personal) ?? null)
  const current = computed(() => projects.value.find((p) => p.id === currentId.value) ?? personal.value)
  // The switcher is only worth showing once there is something to switch to.
  const hasShared = computed(() => projects.value.some((p) => !p.is_personal))

  function select(id: number) {
    currentId.value = id
    try {
      localStorage.setItem(KEY, String(id))
    } catch {
      // storage blocked: the choice lasts for this tab only
    }
  }

  async function load() {
    try {
      projects.value = (await listProjects()) ?? []
      loadError.value = ''
      if (currentId.value !== null && !projects.value.some((p) => p.id === currentId.value)) currentId.value = null
    } catch (e) {
      loadError.value = e instanceof Error ? e.message : "Couldn't load projects."
    } finally {
      loaded.value = true
    }
  }

  async function create(name: string) {
    const p = await createProject(name)
    projects.value = [...projects.value, p]
    select(p.id)
    return p
  }

  function upsert(p: Project) {
    projects.value = projects.value.some((x) => x.id === p.id) ? projects.value.map((x) => (x.id === p.id ? p : x)) : [...projects.value, p]
  }

  function drop(id: number) {
    projects.value = projects.value.filter((p) => p.id !== id)
    if (currentId.value === id) currentId.value = null
  }

  function reset() {
    projects.value = []
    loaded.value = false
  }

  return { projects, loaded, loadError, currentId, current, personal, hasShared, select, load, create, upsert, drop, reset }
})
