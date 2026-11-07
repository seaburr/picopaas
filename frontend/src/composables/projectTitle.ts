import { nextTick, onMounted, watch, type Ref } from 'vue'

// Tab title for project-scoped pages, e.g. "Apps in Personal · PicoPaaS". The router sets the plain title on
// navigation; this overrides it once the project name is known (and again after mount, in case the router wins).
export function useProjectTitle(base: string, projectName: Ref<string | null | undefined> | { value: string | null | undefined }) {
  const apply = () => {
    document.title = projectName.value ? `${base} in ${projectName.value} · PicoPaaS` : `${base} · PicoPaaS`
  }
  watch(() => projectName.value, apply, { immediate: true })
  onMounted(() => { void nextTick(apply) })
}
