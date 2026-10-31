import { computed, onMounted, toValue, type MaybeRefOrGetter } from 'vue'
import { useProjectsStore } from '../stores/projects'

export const SUSPENDED_TITLE = 'Project is suspended'

// Suspension state of one project, from the projects store (refreshed once when the page mounts, so a
// suspend or unsuspend shows up without a full reload). Pages use `suspended` to disable write controls;
// the API's 403 is the real guard.
export function useProjectSuspension(projectId: MaybeRefOrGetter<number | null | undefined>) {
  const store = useProjectsStore()
  onMounted(() => store.load())
  const project = computed(() => {
    const id = toValue(projectId)
    return id == null ? null : (store.projects.find((p) => p.id === id) ?? null)
  })
  const suspended = computed(() => project.value?.status === 'suspended')
  return { project, suspended }
}
