<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useProjectTitle } from '../composables/projectTitle'
import { useRouter } from 'vue-router'
import { projectLabel, projectNewApp } from '../utils/paths'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import AppStatusNote from '../components/AppStatusNote.vue'
import KaisenTag from '../components/KaisenTag.vue'
import HostLink from '../components/HostLink.vue'
import IconButton from '../components/IconButton.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError } from '../api/client'
import { appStatus } from '../composables/statusTone'
import { eventNote, primaryHost, shortAge, fullTime, SAMPLE_APP, useNow } from '../composables/appUi'
import { useAppsStore } from '../stores/apps'
import { useAuthStore } from '../stores/auth'
import { useProjectsStore } from '../stores/projects'
import { useToastStore } from '../stores/toast'
import type { App } from '../types'

const props = defineProps<{ pid: string }>()
const store = useAppsStore()
const auth = useAuthStore()
const projects = useProjectsStore()
const now = useNow()
const toast = useToastStore()
const router = useRouter()
const loading = ref(true)
const error = ref('')
const restarting = ref<number | null>(null)

const pidNum = computed(() => Number(props.pid))
const project = computed(() => projects.projects.find((p) => p.id === pidNum.value) ?? null)
const limit = computed(() => project.value?.app_limit_effective ?? auth.user?.app_limit_effective ?? auth.user?.app_limit ?? 0)
const atLimit = computed(() => limit.value > 0 && store.apps.length >= limit.value)

// Poll while anything is queued, deploying or failed (a failed app can recover on its own).
let timer: ReturnType<typeof setInterval> | null = null
function inFlight() {
  return store.apps.some((a) => a.updating || a.status === 'pending' || a.status === 'deploying' || a.status === 'error')
}
function syncPolling() {
  if (inFlight() && !timer) {
    timer = setInterval(async () => {
      try {
        await store.load(pidNum.value)
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
  const forPid = pidNum.value
  // Coming back to the same project: show its list at once and refresh quietly. Another project: never show the
  // previous project's apps while this one loads.
  const cached = store.loaded && store.pid === forPid
  if (!cached) store.clear()
  loading.value = !cached
  error.value = ''
  try {
    if (!projects.loaded) await projects.load()
    await store.load(forPid)
  } catch (e) {
    if (forPid !== pidNum.value) return // superseded by a newer project; its load owns the state
    error.value = e instanceof ApiError ? e.message : "Couldn't load your apps."
  } finally {
    if (forPid === pidNum.value) loading.value = false
  }
}
onMounted(load)
// The route guard selects the project in the path; a change of :pid reloads the list.
watch(() => props.pid, () => { if (projects.loaded) load() })

const pendingRestart = ref<App | null>(null)
function askRestart(app: App) {
  if (restarting.value === app.id) return
  pendingRestart.value = app
}
async function restart(app: App) {
  pendingRestart.value = null
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
  router.push({ path: projectNewApp(pidNum.value), query: { sample: '1' } })
}

function overCapTip(app: App) {
  const what = [app.quota?.db?.over_quota ? 'database' : '', app.quota?.storage?.over_quota ? 'blob storage' : ''].filter(Boolean)
  return `Over its cap (${what.join(' and ')}): read-only until space is freed.`
}

const projectName = computed(() => projectLabel(project.value))
useProjectTitle('Apps', projectName)
const noteRow = (a: App) => !!a.last_event || ['suspended', 'pending', 'deploying', 'error'].includes(appStatus(a))

// Attached add-ons: PSQL | MySQL | Disk | Blob.
const isPg = (a: App) => !!a.want_db && a.db_engine === 'pg'
const isMysql = (a: App) => !!a.want_db && a.db_engine !== 'pg'
const addOns = (a: App) => [isPg(a) && 'PSQL', isMysql(a) && 'MySQL', !!a.volume && 'Disk', !!a.want_storage && 'Blob'].filter(Boolean).join(' · ')
const MARKS = [
  { key: 'pg', head: 'PSQL', title: 'PostgreSQL database', on: isPg },
  { key: 'mysql', head: 'MySQL', title: 'MySQL database', on: isMysql },
  { key: 'disk', head: 'Disk', title: 'Persistent disk (volume)', on: (a: App) => !!a.volume },
  { key: 'blob', head: 'Blob', title: 'Blob storage (bucket)', on: (a: App) => !!a.want_storage },
]
</script>

<template>
  <main id="main" tabindex="-1" class="page-main">
    <nav class="meta mb-2" aria-label="Breadcrumb">
      <RouterLink :to="`/projects/${pid}`" class="link">{{ projectName || 'Project' }}</RouterLink><span aria-hidden="true"> / </span>Apps
    </nav>
    <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
      <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 class="page-title">Apps<template v-if="projectName"> in {{ projectName }}</template></h1>
        <span v-if="!loading && limit" class="meta mono">{{ store.apps.length }} of {{ limit }}</span>
      </div>
      <div v-if="store.apps.length > 0" class="flex items-center gap-3">
        <span v-if="atLimit" class="meta">App limit reached. Ask an administrator to raise it.</span>
        <IconButton v-if="atLimit" label="New app (app limit reached)" text="New app" disabled />
        <IconButton v-else label="New app" text="New app" :to="projectNewApp(pidNum)" />
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
        <RouterLink :to="projectNewApp(pidNum)" class="btn-primary">New app</RouterLink>
        <button type="button" class="btn-secondary" @click="trySample">Try a sample app</button>
        <RouterLink to="/guide/user" class="btn-ghost">Read the guide</RouterLink>
      </div>
      <p class="help">The sample deploys podinfo (<span class="mono">{{ SAMPLE_APP.image }}</span>, port <span class="mono">{{ SAMPLE_APP.port }}</span>), a tiny demo web app.</p>
    </div>

    <template v-else-if="store.apps.length">
      <!-- Wide: one table. -->
      <div class="overflow-x-auto max-md:hidden">
        <table class="tbl min-w-[1000px]">
          <caption class="sr-only">Apps</caption>
          <colgroup>
            <col class="w-[18%]"><col class="w-[8%]"><col class="w-[19%]"><col class="w-[4.5%]"><col class="w-[4.5%]"><col class="w-[4.5%]"><col class="w-[4.5%]"><col class="w-[17%]"><col class="w-[5%]"><col class="w-[15%]">
          </colgroup>
          <thead>
            <tr>
              <th>Name</th><th>Status</th><th>Image</th>
              <th v-for="m in MARKS" :key="m.key" class="px-0.5! text-center" :title="m.title">{{ m.head }}</th>
              <th>Host</th><th class="text-right">Age</th><th class="text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="app in store.apps" :key="app.id">
              <tr :class="noteRow(app) ? '[&>td]:border-b-0' : ''">
                <td>
                  <div class="flex min-w-0 items-center gap-1.5">
                    <span class="mono min-w-0 truncate font-medium text-white" :title="app.name">{{ app.name }}</span>
                    <KaisenTag v-if="app.kaisen_build_id" :to="`/kaisen/${app.kaisen_build_id}`" class="shrink-0" />
                  </div>
                </td>
                <td>
                  <StatusBadge :status="appStatus(app)" />
                  <span v-if="app.quota?.db?.over_quota || app.quota?.storage?.over_quota" class="block text-xs text-danger-light" :title="overCapTip(app)">over cap</span>
                </td>
                <td class="mono truncate" :title="app.image">{{ app.image }}</td>
                <td v-for="m in MARKS" :key="m.key" class="px-0.5! text-center">
                  <template v-if="m.on(app)"><span aria-hidden="true">✓</span><span class="sr-only">Yes</span></template>
                </td>
                <td class="min-w-0"><HostLink compact :hostname="primaryHost(app)" /></td>
                <td class="mono whitespace-nowrap text-right" :title="fullTime(app.created_at)">{{ shortAge(app.created_at, now) }}</td>
                <td>
                  <div class="row-acts [&>*]:ml-0! flex-nowrap gap-x-2.5 whitespace-nowrap">
                    <RouterLink :to="`/apps/${app.id}`" class="row-act" :aria-label="`View health of ${app.name}`">Health</RouterLink>
                    <button type="button" class="row-act" :aria-label="app.status === 'suspended' ? `Restart ${app.name} (unavailable while suspended)` : `Restart ${app.name}`" :disabled="app.status === 'suspended' || restarting === app.id" @click="askRestart(app)">Restart</button>
                    <span v-if="app.status === 'suspended'" class="row-act" aria-disabled="true" role="link" :aria-label="`Settings for ${app.name} (suspended: contact the administrator)`">Settings</span>
                    <RouterLink v-else :to="`/apps/${app.id}/edit`" class="row-act" :aria-label="`Settings for ${app.name}`">Settings</RouterLink>
                  </div>
                </td>
              </tr>
              <tr v-if="noteRow(app)">
                <td colspan="10" class="!pt-0 text-sm">
                  <p v-if="app.last_event" class="meta">{{ eventNote(app.last_event, now) }}</p>
                  <AppStatusNote v-if="['suspended', 'pending', 'deploying', 'error'].includes(appStatus(app))" compact :app="app" :restarting="restarting === app.id" @restart="askRestart(app)" />
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
            <span class="mono min-w-0 truncate text-sm font-medium text-white" :title="app.name">{{ app.name }}</span>
            <StatusBadge :status="appStatus(app)" class="shrink-0" />
          </div>
          <p class="mono mt-0.5 truncate text-zinc-400" :title="app.image">{{ app.image }}</p>
          <div class="mt-0.5"><HostLink compact :hostname="primaryHost(app)" /></div>
          <p class="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
            <span v-if="addOns(app)"><span class="text-zinc-400">Add-ons </span><span class="mono">{{ addOns(app) }}</span></span>
            <span :title="fullTime(app.created_at)"><span class="text-zinc-400">Age </span><span class="mono">{{ shortAge(app.created_at, now) }}</span></span>
            <span v-if="app.quota?.db?.over_quota || app.quota?.storage?.over_quota" class="text-danger-light" :title="overCapTip(app)">over cap</span>
            <KaisenTag v-if="app.kaisen_build_id" :to="`/kaisen/${app.kaisen_build_id}`" />
          </p>
          <p v-if="app.last_event" class="meta mt-1">{{ eventNote(app.last_event, now) }}</p>
          <div v-if="['suspended', 'pending', 'deploying', 'error'].includes(appStatus(app))" class="mt-1">
            <AppStatusNote compact :app="app" :restarting="restarting === app.id" @restart="askRestart(app)" />
          </div>
          <div class="row-acts mt-1 justify-start gap-x-5">
            <RouterLink :to="`/apps/${app.id}`" class="row-act max-md:min-h-11!" :aria-label="`View health of ${app.name}`">Health</RouterLink>
            <button type="button" class="row-act max-md:min-h-11!" :aria-label="`Restart ${app.name}`" :disabled="app.status === 'suspended' || restarting === app.id" @click="askRestart(app)">Restart</button>
            <span v-if="app.status === 'suspended'" class="row-act max-md:min-h-11!" aria-disabled="true" role="link" :aria-label="`Settings for ${app.name} (suspended)`">Settings</span>
            <RouterLink v-else :to="`/apps/${app.id}/edit`" class="row-act max-md:min-h-11!" :aria-label="`Settings for ${app.name}`">Settings</RouterLink>
          </div>
        </li>
      </ul>
    </template>

    <ConfirmDialog
      :open="!!pendingRestart"
      title="Restart app?"
      :message="pendingRestart ? `${pendingRestart.name} restarts now and is briefly unavailable while the pod rolls.` : ''"
      confirm-label="Restart"
      @confirm="pendingRestart && restart(pendingRestart)"
      @cancel="pendingRestart = null"
    />
  </main>
</template>
