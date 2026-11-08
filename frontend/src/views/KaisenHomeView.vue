<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useProjectTitle } from '../composables/projectTitle'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import HostLink from '../components/HostLink.vue'
import IconButton from '../components/IconButton.vue'
import PreviewBadge from '../components/PreviewBadge.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError } from '../api/client'
import { relTime, useNow } from '../composables/appUi'
import { fmtUsd, isActive } from '../composables/kaisen'
import { useAuthStore } from '../stores/auth'
import { useKaisenStore } from '../stores/kaisen'
import { useProjectsStore } from '../stores/projects'
import { projectKaisen, projectLabel } from '../utils/paths'
import { useToastStore } from '../stores/toast'
import SuspendedBanner from '../components/SuspendedBanner.vue'
import { SUSPENDED_TITLE, useProjectSuspension } from '../composables/projectSuspension'
import type { KaisenBuild } from '../types'

const props = defineProps<{ pid: string }>()
const store = useKaisenStore()
const projects = useProjectsStore()
const auth = useAuthStore()
const toast = useToastStore()
const now = useNow()
const loading = ref(true)
const error = ref('')

const pidNum = computed(() => Number(props.pid))
const limit = computed(() => curProject.value?.app_limit_effective ?? auth.user?.app_limit_effective ?? auth.user?.app_limit ?? 0)
const atLimit = computed(() => limit.value > 0 && store.builds.filter((b) => b.app_id).length >= limit.value)
const noKey = computed(() => !!store.settings && !store.settings.anthropic_key.set)
const canEdit = computed(() => !!store.settings?.can_edit)
const { project: curProject, suspended: projSuspended } = useProjectSuspension(() => pidNum.value)
const projName = computed(() => projectLabel(curProject.value))
useProjectTitle('Kaisen builds', projName)
const blocked = computed(() => projSuspended.value || noKey.value || atLimit.value)
const blockedWhy = computed(() => (projSuspended.value ? 'Project is suspended' : noKey.value ? (canEdit.value ? 'Add an Anthropic key in Kaisen settings first' : 'Ask an owner to add an Anthropic API key') : 'App limit reached. Ask an administrator to raise it.'))

let timer: ReturnType<typeof setInterval> | null = null
function syncPolling() {
  const busy = store.builds.some((b) => isActive(b.status))
  if (busy && !timer) {
    timer = setInterval(async () => {
      try {
        await store.refreshBuilds(pidNum.value)
      } catch {
        // keep last known state
      }
    }, 4000)
  } else if (!busy && timer) {
    clearInterval(timer)
    timer = null
  }
}
watch(() => store.builds, syncPolling, { deep: true })
onUnmounted(() => {
  if (timer) clearInterval(timer)
  timer = null
})

async function load() {
  const forPid = pidNum.value
  // Same project as last time: show its builds at once and refresh quietly. Another project: start empty.
  const cached = store.pid === forPid
  if (!cached) store.clear()
  loading.value = !cached
  error.value = ''
  try {
    if (!projects.loaded) await projects.load()
    await store.load(forPid)
  } catch (e) {
    if (forPid !== pidNum.value) return
    error.value = e instanceof ApiError ? e.message : "Couldn't load your builds."
  } finally {
    if (forPid === pidNum.value) loading.value = false
  }
}
onMounted(load)
watch(() => props.pid, () => { if (projects.loaded) load() })

const pendingDelete = ref<KaisenBuild | null>(null)
const alsoApp = ref(false)
const deleting = ref(false)
async function confirmDelete() {
  const b = pendingDelete.value
  if (!b) return
  deleting.value = true
  try {
    await store.remove(b.id, alsoApp.value)
    toast.success(`Deleted build ${b.name}.`)
    pendingDelete.value = null
  } catch (e) {
    toast.error(`Couldn't delete ${b.name}: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    deleting.value = false
  }
}
function askDelete(b: KaisenBuild) {
  alsoApp.value = false
  pendingDelete.value = b
}
</script>

<template>
  <div>
    <main id="main" tabindex="-1" class="page-main">
      <SuspendedBanner :project="curProject" kaisen />
      <nav class="meta mb-2" aria-label="Breadcrumb">
        <RouterLink :to="`/projects/${pid}`" class="link">{{ projName || 'Project' }}</RouterLink><span aria-hidden="true"> / </span>Kaisen
      </nav>
      <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 class="page-title">Kaisen builds<template v-if="projName"> in {{ projName }}</template></h1>
          <PreviewBadge />
          <span v-if="!loading && store.builds.length" class="meta"><span class="mono">{{ store.builds.length }}</span> {{ store.builds.length === 1 ? 'build' : 'builds' }}</span>
        </div>
        <div class="flex items-center gap-2">
          <span v-if="atLimit && store.builds.length" class="meta mr-2">App limit reached. Ask an administrator to raise it.</span>
          <IconButton label="Kaisen settings" text="Settings" :to="projectKaisen(pidNum, '/settings')" />
          <template v-if="store.builds.length > 0">
            <IconButton v-if="blocked" :label="`New build (${blockedWhy})`" text="New build" disabled />
            <IconButton v-else label="New build" text="New build" :to="projectKaisen(pidNum, '/new')" />
          </template>
        </div>
      </div>

      <div v-if="error" role="alert" class="alert-error mb-4 flex flex-wrap items-center justify-between gap-2">
        <span>{{ error }}</span>
        <IconButton label="Retry" @click="load" />
      </div>

      <ul v-if="loading" class="border-t border-pico-border" aria-busy="true" aria-label="Loading builds">
        <li v-for="n in 3" :key="n" class="border-b border-pico-border py-2">
          <div class="h-4 w-40 animate-pulse rounded-sm bg-pico-card" />
          <div class="mt-2 h-3 w-64 max-w-full animate-pulse rounded-sm bg-pico-card" />
        </li>
      </ul>

      <div v-else-if="!error && store.builds.length === 0" class="sec grid gap-x-12 gap-y-6 pt-4 lg:grid-cols-2">
        <div>
          <h2 class="section-title">Build your first app with Kaisen <PreviewBadge /></h2>
          <p class="mt-1 text-sm text-zinc-300">Tell Kaisen what you want in a few sentences. It writes the code, runs it and deploys it as an app in {{ projName }}. You can download the code or push it to GitHub.</p>
          <p v-if="projSuspended" class="meta mt-3">{{ SUSPENDED_TITLE }}. New builds are paused.</p>
          <div v-else-if="noKey" class="mt-3">
            <p v-if="canEdit" class="alert-warn">Kaisen needs an Anthropic API key added to this project before it can build.</p>
            <p v-else class="alert-warn">Kaisen isn't set up in {{ projName }} yet. Ask an owner to add an Anthropic API key.</p>
            <div class="mt-3">
              <RouterLink v-if="canEdit" :to="projectKaisen(pidNum, '/settings')" class="btn-primary">Open Kaisen settings</RouterLink>
              <IconButton v-else label="New build (Ask an owner to add an Anthropic API key)" text="New build" disabled />
            </div>
          </div>
          <div v-else class="mt-4 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
            <span v-if="atLimit" class="meta">App limit reached. Ask an administrator to raise it.</span>
            <RouterLink v-else :to="projectKaisen(pidNum, '/new')" class="btn-primary">New build</RouterLink>
            <RouterLink to="/guide/user" class="btn-ghost">Read the guide</RouterLink>
          </div>
        </div>
        <div>
          <h2 class="section-title">How it works</h2>
          <table class="tbl mt-2">
            <tbody>
              <tr><th scope="row" class="w-28">1. Describe</th><td class="!whitespace-normal">Say what the app should do. Kaisen asks if it needs to know something.</td></tr>
              <tr><th scope="row">2. Build</th><td class="!whitespace-normal">It writes the code, runs it and fixes what fails. Your replies steer it.</td></tr>
              <tr><th scope="row">3. Deploy</th><td class="!whitespace-normal">It ships the result as an app in {{ projName }}. Keep iterating, download the code or push it to GitHub.</td></tr>
              <tr><th scope="row">4. Iterate</th><td class="!whitespace-normal">Describe the changes you want to refine your app.</td></tr>
              <tr v-if="store.spend"><th scope="row">Spend</th><td class="!whitespace-normal">Last 30 days <span class="mono">{{ fmtUsd(store.spend.last30_usd) }}</span><template v-if="store.spend.cap_day">, daily cap <span class="mono">{{ fmtUsd(store.spend.cap_day) }}</span> (today <span class="mono">{{ fmtUsd(store.spend.today_usd) }}</span>)</template>, from {{ projName }}'s Anthropic key.</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <ul v-else-if="store.builds.length" class="border-t border-pico-border md:hidden" aria-label="Builds">
        <li v-for="b in store.builds" :key="b.id" class="border-b border-pico-border py-2">
          <div class="flex items-center justify-between gap-3">
            <RouterLink :to="`/kaisen/${b.id}`" class="min-w-0 truncate font-medium text-white hover:underline" :title="b.name">{{ b.name }}</RouterLink>
            <StatusBadge kind="build" :status="b.status" />
          </div>
          <div v-if="b.app_hostname" class="mt-0.5 truncate"><HostLink compact :hostname="b.app_hostname" /></div>
          <p class="meta mt-0.5"><span v-if="b.last_run_at">Last run <span class="mono">{{ relTime(b.last_run_at, now) }}</span> · </span><span class="mono">{{ b.file_count }}</span> {{ b.file_count === 1 ? 'file' : 'files' }} · <span class="mono">{{ fmtUsd(b.cost_usd) }}</span></p>
          <p v-if="b.status === 'waiting' || b.open_questions > 0" class="meta">Kaisen is waiting for your answer. <RouterLink :to="`/kaisen/${b.id}`" class="link">Open the build</RouterLink></p>
          <p v-if="(b.status === 'error' || b.status === 'stopped') && b.last_error" class="truncate text-xs text-danger-light" :title="b.last_error">{{ b.last_error }}</p>
          <div class="row-acts !justify-start gap-x-4">
            <RouterLink v-if="b.app_id" :to="`/apps/${b.app_id}`" class="row-act max-md:!min-h-[44px]" :aria-label="`View app health for ${b.name}`">Health</RouterLink>
            <RouterLink :to="`/kaisen/${b.id}`" class="row-act max-md:!min-h-[44px]" :aria-label="`Open build ${b.name}`">Open</RouterLink>
            <button type="button" class="row-act row-act-danger max-md:!min-h-[44px]" :aria-label="`Delete build ${b.name}`" @click="askDelete(b)">Delete</button>
          </div>
        </li>
      </ul>

      <div v-if="!loading && store.builds.length" class="max-md:hidden">
        <table class="tbl">
          <caption class="sr-only">Builds</caption>
          <colgroup>
            <col />
            <col class="w-28" />
            <col class="w-16" />
            <col class="w-24" />
            <col class="w-20" />
            <col class="w-48" />
          </colgroup>
          <thead>
            <tr>
              <th scope="col">Build</th>
              <th scope="col">Status</th>
              <th scope="col" class="text-right">Files</th>
              <th scope="col">Last run</th>
              <th scope="col" class="text-right">Cost</th>
              <th scope="col"><span class="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="b in store.builds" :key="b.id">
              <td class="min-w-0 !whitespace-normal">
                <RouterLink :to="`/kaisen/${b.id}`" class="block truncate font-medium text-white hover:underline" :title="b.name">{{ b.name }}</RouterLink>
                <div v-if="b.app_hostname" class="truncate"><HostLink compact :hostname="b.app_hostname" /></div>
                <p v-if="b.status === 'waiting' || b.open_questions > 0" class="meta">Kaisen is waiting for your answer. <RouterLink :to="`/kaisen/${b.id}`" class="link">Open the build</RouterLink></p>
                <p v-if="(b.status === 'error' || b.status === 'stopped') && b.last_error" class="truncate text-xs text-danger-light" :title="b.last_error">{{ b.last_error }}</p>
              </td>
              <td><StatusBadge kind="build" :status="b.status" /></td>
              <td class="mono text-right">{{ b.file_count }}</td>
              <td class="mono">{{ b.last_run_at ? relTime(b.last_run_at, now) : '' }}</td>
              <td class="mono text-right">{{ fmtUsd(b.cost_usd) }}</td>
              <td>
                <div class="row-acts">
                  <RouterLink v-if="b.app_id" :to="`/apps/${b.app_id}`" class="row-act" :aria-label="`View app health for ${b.name}`">Health</RouterLink>
                  <RouterLink :to="`/kaisen/${b.id}`" class="row-act" :aria-label="`Open build ${b.name}`">Open</RouterLink>
                  <button type="button" class="row-act row-act-danger" :aria-label="`Delete build ${b.name}`" @click="askDelete(b)">Delete</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p v-if="!loading && !error && store.spend && store.builds.length" class="help mt-3">
        Kaisen is in preview. Builds spend from {{ projName }}'s Anthropic key. Last 30 days: <span class="mono">{{ fmtUsd(store.spend.last30_usd) }}</span><template v-if="store.spend.cap_day"> · daily cap <span class="mono">{{ fmtUsd(store.spend.cap_day) }}</span> (today <span class="mono">{{ fmtUsd(store.spend.today_usd) }}</span>)</template>
      </p>
    </main>

    <ConfirmDialog
      :open="!!pendingDelete"
      title="Delete build?"
      message="This removes the build, its files and run history. Spend totals stay in your usage."
      danger
      confirm-label="Delete build"
      :require-text="pendingDelete?.name"
      :busy="deleting"
      busy-label="Deleting…"
      @confirm="confirmDelete"
      @cancel="pendingDelete = null"
    >
      <label v-if="pendingDelete?.app_id" class="mt-3 flex items-center gap-2 text-sm text-zinc-200">
        <input v-model="alsoApp" type="checkbox" class="check" /> Also delete the app ({{ pendingDelete.name }})
      </label>
    </ConfirmDialog>
  </div>
</template>
