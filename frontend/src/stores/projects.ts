import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { createProject, listProjects } from '../api/projects'
import type { Project } from '../types'
import { projectApps } from '../utils/paths'

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
  // Where "my apps" lives for links that have no project of their own (home, not-found, invite): the current project's list.
  const appsHome = computed(() => (current.value ? projectApps(current.value.id) : '/apps'))
  // The project picker is only worth showing once there is something to switch to.
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
      const apply = (list: Project[]) => {
        projects.value = list ?? []
        if (currentId.value !== null && !projects.value.some((p) => p.id === currentId.value)) currentId.value = null
      }
      // A recent copy shows at once; a changed answer lands when the background refresh returns.
      apply((await listProjects(apply)) ?? [])
      loadError.value = ''
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

  return { projects, loaded, loadError, currentId, current, personal, appsHome, hasShared, select, load, create, upsert, drop, reset }
})
