<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import AppStatusNote from '../components/AppStatusNote.vue'
import DeleteAppDialog from '../components/DeleteAppDialog.vue'
import HostLink from '../components/HostLink.vue'
import IconButton from '../components/IconButton.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError } from '../api/client'
import { eventNote, primaryHost, relTime, fullTime, SAMPLE_APP, useNow } from '../composables/appUi'
import { useAppsStore } from '../stores/apps'
import { useAuthStore } from '../stores/auth'
import { useProjectsStore } from '../stores/projects'
import { useToastStore } from '../stores/toast'
import type { App } from '../types'

const store = useAppsStore()
const auth = useAuthStore()
const projects = useProjectsStore()
const now = useNow()
const toast = useToastStore()
const router = useRouter()
const loading = ref(true)
const error = ref('')
const restarting = ref<number | null>(null)

const limit = computed(() => projects.current?.app_limit_effective ?? auth.user?.app_limit_effective ?? auth.user?.app_limit ?? 0)
const atLimit = computed(() => limit.value > 0 && store.apps.length >= limit.value)

// Poll while anything is queued, deploying or failed (a failed app can recover on its own).
let timer: ReturnType<typeof setInterval> | null = null
function inFlight() {
  return store.apps.some((a) => a.status === 'pending' || a.status === 'deploying' || a.status === 'error')
}
function syncPolling() {
  if (inFlight() && !timer) {
    timer = setInterval(async () => {
      try {
        await store.load()
      } catch {
        // keep last known state; next tick retries
      }
    }, 4000)
  } else if (!inFlight() && timer) {
    clearInterval(timer)
    timer = null
  }
}
watch(() => store.apps, syncPolling, { deep: true })
onUnmounted(() => {
  if (timer) clearInterval(timer)
  timer = null
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    if (!projects.loaded) await projects.load()
    await store.load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load your apps."
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch(() => projects.currentId, (n, o) => { if (o !== n && projects.loaded) load() })

const pendingDelete = ref<App | null>(null)
const deleting = ref(false)
async function confirmDelete() {
  const app = pendingDelete.value
  if (!app) return
  deleting.value = true
  try {
    await store.remove(app.id)
    toast.success(`Deleted ${app.name}.`)
    pendingDelete.value = null
  } catch (e) {
    toast.error(`Couldn't delete ${app.name}: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    deleting.value = false
  }
}

async function restart(app: App) {
  restarting.value = app.id
  try {
    await store.restart(app.id)
    toast.success(`Restarting ${app.name}.`)
  } catch (e) {
    toast.error(
      e instanceof ApiError && e.status === 404
        ? "Restart isn't available on this platform version yet."
        : `Couldn't restart ${app.name}: ${e instanceof ApiError ? e.message : 'unknown error'}`,
    )
  } finally {
    restarting.value = null
  }
}

function trySample() {
  router.push({ path: '/apps/new', query: { sample: '1' } })
}

function overCapTip(app: App) {
  const what = [app.quota?.db?.over_quota ? 'database' : '', app.quota?.storage?.over_quota ? 'blob storage' : ''].filter(Boolean)
  return `Over its cap (${what.join(' and ')}): read-only until space is freed.`
}

const projectName = computed(() => (projects.current ? (projects.current.is_personal ? 'Personal' : projects.current.name) : ''))
const noteRow = (a: App) => !!a.last_event || ['suspended', 'pending', 'deploying', 'error'].includes(a.status)

// List cells. Limits come from the API (app.resources / app.quota), never constants.
const limits = computed(() => auth.user?.limits)
const memText = (a: App) => `${a.resources?.mem_limit_mi ?? limits.value?.mem_limit_mi ?? '–'} MB`
const cpuText = (a: App) => `${a.resources?.cpu_limit_m ?? limits.value?.cpu_limit_m ?? '–'}m`
const MB = 1024 * 1024
const mbNum = (n: number) => (n >= 10 * MB || n === 0 ? Math.round(n / MB) : +(n / MB).toFixed(1))
function usageText(u: { size_bytes: number; limit_bytes: number } | null | undefined, on: boolean): string {
  if (!on) return '–'
  if (!u) return 'attached'
  return `${mbNum(u.size_bytes)} / ${mbNum(u.limit_bytes)} MB`
}
function usageTone(u: { size_bytes: number; limit_bytes: number; over_quota: boolean } | null | undefined): string {
  if (!u) return ''
  if (u.over_quota) return 'text-danger-light'
  return u.limit_bytes && u.size_bytes / u.limit_bytes >= 0.8 ? 'text-amber-400' : ''
}
</script>

<template>
  <main id="main" tabindex="-1" class="page-main">
    <p v-if="projectName" class="meta">{{ projectName }}</p>
    <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
      <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 class="page-title">Apps</h1>
        <span v-if="!loading && limit" class="meta mono">{{ store.apps.length }} of {{ limit }}</span>
      </div>
      <div v-if="store.apps.length > 0" class="flex items-center gap-3">
        <span v-if="atLimit" class="meta">App limit reached. Ask an administrator to raise it.</span>
        <IconButton v-if="atLimit" label="New app (app limit reached)" text="New app" disabled />
        <IconButton v-else label="New app" text="New app" to="/apps/new" />
      </div>
    </div>

    <p v-if="projects.loaded && !projects.hasShared" class="help mb-3">
      Want to share apps with others? <RouterLink to="/projects" class="underline">Create a project.</RouterLink>
    </p>

    <div v-if="error" role="alert" class="alert-error mb-4 flex flex-wrap items-center justify-between gap-2">
      <span>{{ error }}</span>
      <IconButton label="Retry" @click="load" />
    </div>

    <ul v-if="loading" class="border-t border-pico-border" aria-busy="true" aria-label="Loading apps">
      <li v-for="n in 3" :key="n" class="border-b border-pico-border py-3">
        <div class="h-4 w-40 animate-pulse rounded-sm bg-pico-muted" />
        <div class="mt-2 h-3 w-64 max-w-full animate-pulse rounded-sm bg-pico-muted" />
      </li>
    </ul>

    <div v-else-if="!error && store.apps.length === 0" class="sec sec-pad max-w-2xl">
      <h2 class="section-title">Deploy your first app</h2>
      <p class="mt-1 text-sm text-zinc-300">Point us at a public Docker image and the port it listens on. You'll have a live https URL in about a minute.</p>
      <div class="mt-5 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
        <RouterLink to="/apps/new" class="btn-primary">New app</RouterLink>
        <button type="button" class="btn-secondary" @click="trySample">Try a sample app</button>
        <RouterLink to="/guide/user" class="btn-ghost">Read the guide</RouterLink>
      </div>
      <p class="help">The sample deploys podinfo (<span class="mono">{{ SAMPLE_APP.image }}</span>, port <span class="mono">{{ SAMPLE_APP.port }}</span>), a tiny demo web app.</p>
    </div>

    <template v-else-if="store.apps.length">
      <!-- Wide: one table. -->
      <div class="overflow-x-auto max-md:hidden">
        <table class="tbl min-w-[1040px]">
          <caption class="sr-only">Apps</caption>
          <colgroup>
            <col class="w-[9%]"><col class="w-[8%]"><col class="w-[17%]"><col class="w-[4%]"><col class="w-[7%]"><col class="w-[6%]"><col class="w-[7%]"><col class="w-[7%]"><col class="w-[14%]"><col class="w-[6%]"><col class="w-[15%]">
          </colgroup>
          <thead>
            <tr>
              <th>Name</th><th>Status</th><th>Image</th><th class="text-right">Port</th><th class="text-right">Memory limit</th><th class="text-right">CPU limit</th>
              <th class="text-right">Database</th><th class="text-right">Bucket</th><th>Host</th><th>Created</th><th class="text-right"><span class="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            <template v-for="app in store.apps" :key="app.id">
              <tr :class="noteRow(app) ? '[&>td]:border-b-0' : ''">
                <td class="truncate">
                  <RouterLink :to="`/apps/${app.id}`" class="mono font-medium text-white hover:underline" :title="app.name">{{ app.name }}</RouterLink>
                  <RouterLink v-if="app.kaisen_build_id" :to="`/kaisen/${app.kaisen_build_id}`" class="meta ml-1.5 underline" title="Built with Kaisen (Preview)">Kaisen<span class="sr-only"> (built with Kaisen, Preview)</span></RouterLink>
                </td>
                <td>
                  <StatusBadge :status="app.status" />
                  <span v-if="app.quota?.db?.over_quota || app.quota?.storage?.over_quota" class="block text-xs text-danger-light" :title="overCapTip(app)">over cap</span>
                </td>
                <td class="mono truncate" :title="app.image">{{ app.image }}</td>
                <td class="mono text-right">{{ app.port }}</td>
                <td class="mono text-right">{{ memText(app) }}</td>
                <td class="mono text-right">{{ cpuText(app) }}</td>
                <td class="mono text-right" :class="usageTone(app.quota?.db)" :title="app.want_db ? undefined : 'No database'">{{ usageText(app.quota?.db, app.want_db) }}</td>
                <td class="mono text-right" :class="usageTone(app.quota?.storage)" :title="app.want_storage ? undefined : 'No bucket'">{{ usageText(app.quota?.storage, app.want_storage) }}</td>
                <td class="min-w-0"><HostLink compact :hostname="primaryHost(app)" /></td>
                <td class="mono whitespace-nowrap" :title="fullTime(app.created_at)">{{ relTime(app.created_at, now) }}</td>
                <td>
                  <div class="row-acts [&>*]:ml-0! gap-x-2.5 whitespace-nowrap">
                    <RouterLink :to="`/apps/${app.id}`" class="row-act" :aria-label="`View health of ${app.name}`">Health</RouterLink>
                    <button type="button" class="row-act" :aria-label="app.status === 'suspended' ? `Restart ${app.name} (unavailable while suspended)` : `Restart ${app.name}`" :disabled="app.status === 'suspended' || restarting === app.id" @click="restart(app)">Restart</button>
                    <span v-if="app.status === 'suspended'" class="row-act" aria-disabled="true" role="link" :aria-label="`Settings for ${app.name} (suspended: contact the administrator)`">Settings</span>
                    <RouterLink v-else :to="`/apps/${app.id}/edit`" class="row-act" :aria-label="`Settings for ${app.name}`">Settings</RouterLink>
                    <button type="button" class="row-act row-act-danger" :aria-label="`Delete ${app.name}`" @click="pendingDelete = app">Delete</button>
                  </div>
                </td>
              </tr>
              <tr v-if="noteRow(app)">
                <td colspan="11" class="!pt-0 text-sm">
                  <p v-if="app.last_event" class="meta">{{ eventNote(app.last_event, now) }}</p>
                  <AppStatusNote v-if="['suspended', 'pending', 'deploying', 'error'].includes(app.status)" compact :app="app" :restarting="restarting === app.id" @restart="restart(app)" />
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>

      <!-- Phone: divider-separated rows. -->
      <ul class="border-t border-pico-border md:hidden">
        <li v-for="app in store.apps" :key="app.id" class="border-b border-pico-border py-2.5">
          <div class="flex items-baseline justify-between gap-2">
            <RouterLink :to="`/apps/${app.id}`" class="mono min-w-0 truncate text-sm font-medium text-white" :title="app.name">{{ app.name }}</RouterLink>
            <StatusBadge :status="app.status" class="shrink-0" />
          </div>
          <p class="mono mt-0.5 truncate text-zinc-400" :title="app.image">{{ app.image }}</p>
          <div class="mt-0.5"><HostLink compact :hostname="primaryHost(app)" /></div>
          <p class="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
            <span><span class="text-zinc-400">Mem limit </span><span class="mono">{{ memText(app) }}</span></span>
            <span><span class="text-zinc-400">DB </span><span class="mono" :class="usageTone(app.quota?.db)">{{ usageText(app.quota?.db, app.want_db) }}</span></span>
            <span><span class="text-zinc-400">Bucket </span><span class="mono" :class="usageTone(app.quota?.storage)">{{ usageText(app.quota?.storage, app.want_storage) }}</span></span>
            <span v-if="app.quota?.db?.over_quota || app.quota?.storage?.over_quota" class="text-danger-light" :title="overCapTip(app)">over cap</span>
            <RouterLink v-if="app.kaisen_build_id" :to="`/kaisen/${app.kaisen_build_id}`" class="underline" title="Built with Kaisen (Preview)">Built with Kaisen</RouterLink>
          </p>
          <p v-if="app.last_event" class="meta mt-1">{{ eventNote(app.last_event, now) }}</p>
          <div v-if="['suspended', 'pending', 'deploying', 'error'].includes(app.status)" class="mt-1">
            <AppStatusNote compact :app="app" :restarting="restarting === app.id" @restart="restart(app)" />
          </div>
          <div class="row-acts mt-1 justify-start gap-x-5">
            <RouterLink :to="`/apps/${app.id}`" class="row-act max-md:min-h-11!" :aria-label="`View health of ${app.name}`">Health</RouterLink>
            <button type="button" class="row-act max-md:min-h-11!" :aria-label="`Restart ${app.name}`" :disabled="app.status === 'suspended' || restarting === app.id" @click="restart(app)">Restart</button>
            <span v-if="app.status === 'suspended'" class="row-act max-md:min-h-11!" aria-disabled="true" role="link" :aria-label="`Settings for ${app.name} (suspended)`">Settings</span>
            <RouterLink v-else :to="`/apps/${app.id}/edit`" class="row-act max-md:min-h-11!" :aria-label="`Settings for ${app.name}`">Settings</RouterLink>
            <button type="button" class="row-act row-act-danger max-md:min-h-11!" :aria-label="`Delete ${app.name}`" @click="pendingDelete = app">Delete</button>
          </div>
        </li>
      </ul>
    </template>

    <DeleteAppDialog :app="pendingDelete" :busy="deleting" @confirm="confirmDelete" @cancel="pendingDelete = null" />
  </main>
</template>
