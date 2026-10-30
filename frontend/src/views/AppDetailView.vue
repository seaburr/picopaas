<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import BackupsCard from '../components/BackupsCard.vue'
import AppNotFound from '../components/AppNotFound.vue'
import AppStatusNote from '../components/AppStatusNote.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import FreeStorageDialog from '../components/FreeStorageDialog.vue'
import UsageBar from '../components/UsageBar.vue'
import HostLink from '../components/HostLink.vue'
import { Download, PackageSearch, Wand2, RefreshCw, RotateCw, Settings, X, Undo2 } from 'lucide-vue-next'
import IconButton from '../components/IconButton.vue'
import NavBar from '../components/NavBar.vue'
import Sparkline from '../components/Sparkline.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { dismissAlert, deleteStorageObjects, getAppEvents, downloadPodEvents, getAppLogs, getImageHistory, getMetricsHistory, type AppLogs } from '../api/apps'
import { ApiError } from '../api/client'
import { stamp } from '../utils/format'
import { uptimeText, dbEngineLabel, eventNote, fullTime, parseTs, primaryHost, servedHosts, relTime, useNow } from '../composables/appUi'
import { refLabel, refsIn } from '../composables/envKeys'
import { useAppsStore } from '../stores/apps'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'
import { DB_REFS, STORAGE_REFS, type App, type AppEvent, type ImageVersion, type MetricsHistory } from '../types'

const props = defineProps<{ id: string }>()
const route = useRoute()
const store = useAppsStore()
const auth = useAuthStore()
const toast = useToastStore()
const now = useNow()

const app = ref<App | null>(null)
const loadError = ref('')
const fetchedAt = ref(Date.now())
const notFound = ref(false)

async function loadApp(quiet = false) {
  try {
    app.value = await store.fetchOne(Number(props.id))
    fetchedAt.value = Date.now()
    if (app.value) {
      store.upsert(app.value)
      document.title = `${app.value.name} · PicoPaaS`
    }
    loadError.value = ''
    loadVersions()
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound.value = true
    else if (!quiet || !app.value) loadError.value = e instanceof ApiError ? e.message : "Couldn't load this app."
  }
}

// Poll while queued / deploying.
let poll: ReturnType<typeof setInterval> | null = null
function syncPoll() {
  const active = app.value && (app.value.status === 'pending' || app.value.status === 'deploying')
  if (active && !poll) poll = setInterval(() => loadApp(true), 3000)
  else if (!active && poll) {
    clearInterval(poll)
    poll = null
  }
}
watch(app, syncPoll)

// Recent events (restarts for disk / memory / crash)
const events = ref<AppEvent[]>([])
async function loadEvents() {
  try {
    events.value = await getAppEvents(Number(props.id))
  } catch {
    events.value = [] // older backend or a transient error: the section just stays empty
  }
}

// Resource usage history (one sample a minute, ~3h kept)
const metrics = ref<MetricsHistory | null>(null)
const podCount = computed(() => Math.max(metrics.value?.replicas ?? 1, metrics.value?.pods ?? 1))
async function loadMetrics() {
  try {
    metrics.value = await getMetricsHistory(Number(props.id))
  } catch {
    metrics.value = null // older backend or a transient error: the panel stays hidden
  }
}
let metricsTimer: ReturnType<typeof setInterval> | null = null

// Recent versions (image history) + rollback
const versions = ref<ImageVersion[]>([])
async function loadVersions() {
  try {
    versions.value = await getImageHistory(Number(props.id))
  } catch {
    versions.value = [] // older backend or a transient error: the section just stays empty
  }
}
const rollbackTarget = ref<ImageVersion | null>(null)
const rollingBack = ref(false)
async function confirmRollback() {
  if (!app.value || !rollbackTarget.value) return
  rollingBack.value = true
  try {
    app.value = (await store.rollback(app.value.id, rollbackTarget.value.digest)) ?? app.value
    toast.success('Rolling back. This takes about a minute.')
    rollbackTarget.value = null
    syncPoll()
    loadVersions()
  } catch (e) {
    toast.error(`Couldn't roll back: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    rollingBack.value = false
  }
}

// Logs
const logs = ref<AppLogs | null>(null)
const logsError = ref('')
const logsBusy = ref(false)
const podSel = ref('') // '' = all pods
const autoRefresh = ref(false)
const dlBusy = ref(false)
const evBusy = ref(false)
const podMeta = computed(() => new Map((logs.value?.pod_info ?? []).map((i) => [i.name, i])))
function podLabel(name: string) {
  const i = podMeta.value.get(name)
  if (!i) return name
  if (i.current) return `${name} (current)`
  if (i.phase === 'Succeeded' || i.phase === 'Failed') return `${name} (finished, ${i.phase.toLowerCase()})`
  return `${name} (old${i.phase && i.phase !== 'Running' ? ', ' + i.phase.toLowerCase() : ''})`
}
const eventsPod = computed(() => podSel.value || ((logs.value?.pods?.length ?? 0) <= 1 ? logs.value?.pod || '' : ''))
async function downloadEvents() {
  const a = app.value
  const pod = eventsPod.value
  if (!a || !pod || evBusy.value) return
  evBusy.value = true
  try {
    await downloadPodEvents(a.id, pod)
  } catch (e) {
    toast.error(`Couldn't download pod events: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    evBusy.value = false
  }
}
// eslint-disable-next-line no-control-regex
const ANSI_RE = /(?:\x1b)?\[[0-9;?]*[ -/]*m|\x1b\[[0-9;?]*[ -/]*[@-~]/g

async function downloadLogs() {
  const a = app.value
  if (!a || dlBusy.value) return
  dlBusy.value = true
  try {
    const r = await getAppLogs(a.id, 1000, podSel.value || undefined)
    const multi = (r.sections?.length ?? 0) > 1
    const lines = (multi ? r.sections!.flatMap((x) => [`=== pod ${x.pod} (restarts ${x.restarts}) ===`, ...x.lines]) : r.lines).map((l) => l.replace(ANSI_RE, ''))
    if (!lines.length && !r.note) {
      toast.error('No logs to download yet.')
      return
    }
    const head = `# ${a.name} | ${a.hostname} | pod ${r.pod || 'none'} | ${new Date().toISOString()}`
    const body = [head, ...(r.note ? [`# ${r.note}`] : []), ...lines].join('\n') + '\n'
    const url = URL.createObjectURL(new Blob([body], { type: 'text/plain;charset=utf-8' }))
    const el = document.createElement('a')
    el.href = url
    el.download = `${a.name.replace(/[^A-Za-z0-9._-]+/g, '-')}-${a.id}-logs.txt`
    document.body.appendChild(el)
    el.click()
    el.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) {
    toast.error(`Couldn't download logs: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    dlBusy.value = false
  }
}

const logText = computed(() => {
  const l = logs.value
  if (!l) return ''
  if (!podSel.value && (l.sections?.length ?? 0) > 1)
    return l.sections!.map((x) => `=== pod ${x.pod} (restarts ${x.restarts}) ===\n${x.lines.join('\n')}${x.note && !x.lines.length ? '(' + x.note + ')' : ''}`).join('\n\n').replace(ANSI_RE, '')
  return l.lines.join('\n').replace(ANSI_RE, '')
})
const logBox = ref<HTMLElement | null>(null)
let logTimer: ReturnType<typeof setInterval> | null = null

async function loadLogs() {
  logsBusy.value = true
  try {
    logs.value = await getAppLogs(Number(props.id), 200, podSel.value || undefined)
    logsError.value = ''
    await nextTick()
    if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
  } catch (e) {
    logsError.value =
      e instanceof ApiError && (e.status === 404 || e.status === 405)
        ? "Couldn't find logs for this app. It may have been deleted."
        : e instanceof ApiError
          ? e.message
          : "Couldn't load logs."
  } finally {
    logsBusy.value = false
  }
}
watch(autoRefresh, (v) => {
  if (logTimer) clearInterval(logTimer)
  logTimer = v ? setInterval(loadLogs, 5000) : null
})

onMounted(async () => {
  await loadApp()
  syncPoll()
  if (app.value && app.value.status !== 'suspended') await loadLogs()
  await loadEvents()
  loadMetrics()
  metricsTimer = setInterval(loadMetrics, 60000)
  if (route.hash === '#logs') document.getElementById('logs')?.scrollIntoView()
})
onBeforeUnmount(() => {
  if (poll) clearInterval(poll)
  if (metricsTimer) clearInterval(metricsTimer)
  if (logTimer) clearInterval(logTimer)
})

// Restart
const restarting = ref(false)
async function restart() {
  if (!app.value) return
  restarting.value = true
  try {
    app.value = (await store.restart(app.value.id)) ?? app.value
    toast.success('Restarting. This takes about a minute.')
    syncPoll()
    setTimeout(loadLogs, 3000)
  } catch (e) {
    toast.error(
      e instanceof ApiError && e.status === 404
        ? "Couldn't restart: this app no longer exists. Reload the page."
        : `Couldn't restart: ${e instanceof ApiError ? e.message : 'unknown error'}`,
    )
  } finally {
    restarting.value = false
  }
}

// Check for a new image (re-resolves the tag; redeploys if it changed)
const checking = ref(false)
const isDigestRef = computed(() => !!app.value && app.value.image.includes('@'))
async function checkImage() {
  if (!app.value) return
  checking.value = true
  try {
    const before = app.value.image_digest
    app.value = (await store.checkImage(app.value.id)) ?? app.value
    toast.success(app.value.image_digest !== before ? 'New image found. Redeploying.' : 'Already on the latest image.')
    syncPoll()
  } catch (e) {
    toast.error(`Couldn't check the image: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    checking.value = false
  }
}
const shortDigest = (d?: string | null) => (d ?? '').replace(/^sha256:/, '').slice(0, 12)




// Usage / over-cap recovery
const freeOpen = ref(false)
const freeBusy = ref(false)
const dbLimitMb = computed(() => Math.round((app.value?.quota?.db?.limit_bytes ?? 50 * 1024 * 1024) / 1024 / 1024))
const storageLimitMb = computed(() => Math.round((app.value?.quota?.storage?.limit_bytes ?? 100 * 1024 * 1024) / 1024 / 1024))
async function confirmFree(body: { prefix: string } | { all: true }) {
  if (!app.value) return
  freeBusy.value = true
  try {
    const r = await deleteStorageObjects(app.value.id, body)
    const left = (r.size_bytes / 1024 / 1024).toFixed(1)
    toast.success(
      r.over_quota
        ? `Deleted ${r.deleted} objects, but the blob storage bucket is still over its cap (${left} MB).`
        : `Deleted ${r.deleted} objects. Writes are coming back; the app restarts to pick up new keys.`,
    )
    if (r.truncated) toast.info('Some objects remain. Run it again to continue.')
    freeOpen.value = false
    await loadApp(true)
    syncPoll()
  } catch (e) {
    toast.error(`Couldn't delete objects: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    freeBusy.value = false
  }
}


const limits = computed(() => auth.user?.limits)
const res = computed(() => {
  const r = app.value?.resources
  return {
    cpu: r?.cpu_limit_m ?? limits.value?.cpu_limit_m ?? 200,
    mem: r?.mem_limit_mi ?? limits.value?.mem_limit_mi ?? 384,
    custom: !!r?.overridden,
  }
})
const hasAddons = computed(() => !!app.value && (app.value.want_db || app.value.want_storage))
const uptime = computed(() => {
  const a = app.value
  if (!a || a.status !== 'running') return 0
  if (typeof a.uptime_seconds === 'number') return Math.max(0, Math.floor(a.uptime_seconds + (now.value - fetchedAt.value) / 1000))
  // Backend field not present: fall back to the newest restart event or creation time.
  const since = a.last_event ? parseTs(a.last_event.at) : parseTs(a.created_at)
  return isNaN(since.getTime()) ? 0 : Math.max(0, Math.floor((now.value - since.getTime()) / 1000))
})
async function dismiss(kind: string) {
  if (!app.value) return
  try {
    app.value = await dismissAlert(app.value.id, kind)
  } catch (e) {
    toast.error(e instanceof ApiError ? e.message : "Couldn't dismiss the alert.")
  }
}
const suspended = computed(() => app.value?.status === 'suspended')

// Which env vars reference add-on credentials (non-secret values only; secrets are never readable).
const usedRefs = computed(() =>
  (app.value?.env ?? []).filter((e) => !e.is_secret).flatMap((e) => refsIn(e.value).map((r) => ({ key: e.key, ref: r }))),
)
const secretCount = computed(() => (app.value?.env ?? []).filter((e) => e.is_secret).length)
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />

    <main id="main" tabindex="-1" class="page-main">
      <AppNotFound v-if="notFound" />

      <div v-else-if="loadError && !app" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
        <span>{{ loadError }}</span>
        <IconButton :icon="RotateCw" label="Retry" @click="loadApp()" />
      </div>

      <div v-else-if="!app" class="space-y-3" aria-busy="true" aria-label="Loading app">
        <div class="h-6 w-48 animate-pulse rounded-sm bg-pico-panel" />
        <div class="h-32 animate-pulse rounded-sm bg-pico-panel" />
      </div>

      <template v-else>
        <nav class="meta mb-2" aria-label="Breadcrumb">
          <RouterLink to="/apps" class="link">Apps</RouterLink><span aria-hidden="true"> / </span>{{ app.name }}
        </nav>

        <header class="mb-3 flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2">
              <h1 class="page-title break-all">{{ app.name }}</h1>
              <StatusBadge :status="app.status" />
              <span v-if="app.kaisen_build_id" class="badge-info" title="Built with Kaisen (Preview)">Built with Kaisen · Preview</span>
            </div>
            <div class="mt-1 flex min-w-0 flex-wrap items-center gap-x-2">
              <HostLink :hostname="primaryHost(app)" class="text-base font-medium" />
              <span v-if="servedHosts(app).length > 1" class="badge-info shrink-0">primary</span>
            </div>
            <ul v-if="servedHosts(app).length > 1" class="mt-1 min-w-0 text-sm text-zinc-400">
              <li v-for="h in servedHosts(app).slice(1)" :key="h" class="min-w-0"><HostLink :hostname="h" /></li>
            </ul>
          </div>
          <div class="flex items-center gap-2">
            <IconButton v-if="app.kaisen_build_id" :icon="Wand2" label="Open in Kaisen" :to="`/kaisen/${app.kaisen_build_id}`" />
            <IconButton v-if="!isDigestRef" :icon="PackageSearch" :label="suspended ? 'Check for new image (suspended)' : checking ? 'Checking…' : 'Check for new image'" :disabled="suspended || checking" @click="checkImage" />
            <IconButton :icon="RotateCw" :label="suspended ? 'Restart (suspended: contact the administrator)' : restarting ? 'Restarting…' : 'Restart'" :disabled="suspended || restarting" @click="restart" />
            <IconButton :icon="Settings" label="Settings" :to="`/apps/${app.id}/edit`" />
          </div>
        </header>

        <div class="space-y-3">
          <section class="card card-pad" aria-labelledby="h-status">
            <h2 id="h-status" class="section-title mb-2">Status</h2>
            <p v-if="app.status === 'running'" class="text-sm text-zinc-200">
              Running and serving traffic.
              <span v-if="uptime" class="text-zinc-400" :title="'Since ' + fullTime(new Date(now - uptime * 1000).toISOString())">Uptime {{ uptimeText(uptime) }}.</span>
            </p>
            <AppStatusNote :app="app" :restarting="restarting" @restart="restart" />
            <p v-if="app.image_update_error" role="alert" class="alert-warn mt-2 flex items-start justify-between gap-2">
              <span>{{ app.image_update_error }}</span>
              <IconButton :icon="X" sm label="Dismiss alert" @click="dismiss('image_update')" />
            </p>
            <p v-if="app.last_event" class="alert-info mt-2 flex items-start justify-between gap-2">
              <span>{{ eventNote(app.last_event, now) }}</span>
              <IconButton :icon="X" sm label="Dismiss alert" @click="dismiss('event')" />
            </p>
          </section>

          <section v-if="metrics && !suspended" class="card card-pad" aria-labelledby="h-metrics">
            <h2 id="h-metrics" class="section-title mb-2">Resource usage <span v-if="podCount > 1" class="meta font-normal">across {{ podCount }} pods</span></h2>
            <p v-if="!metrics.points.length" class="meta">Collecting&hellip; the first points show up within a couple of minutes.</p>
            <div v-else class="grid gap-x-6 gap-y-3 sm:grid-cols-2">
              <Sparkline label="CPU" unit="m" :values="metrics.points.map((p) => p.cpu_m)" :limit="metrics.cpu_capacity_m ?? metrics.cpu_limit_m" />
              <Sparkline label="Mem" unit="Mi" :values="metrics.points.map((p) => p.mem_mi)" :limit="metrics.mem_capacity_mi ?? metrics.mem_limit_mi" />
            </div>
          </section>

          <section v-if="app.quota?.db || app.quota?.storage || app.quota?.volume || app.volume" class="card card-pad" aria-labelledby="h-usage">
            <h2 id="h-usage" class="section-title mb-2">Usage</h2>
            <div class="grid gap-x-6 gap-y-3 sm:grid-cols-2">
              <UsageBar v-if="app.quota?.db" label="Database" :usage="app.quota.db" />
              <UsageBar v-if="app.quota?.storage" label="Blob storage" :usage="app.quota.storage" />
              <UsageBar v-if="app.quota?.volume" label="Volume" :usage="app.quota.volume" />
              <div v-else-if="app.volume" class="flex items-baseline justify-between gap-2 text-sm"><span class="meta">Volume</span><span class="mono text-zinc-400">Measuring…</span></div>
            </div>
            <p v-if="app.quota?.db?.over_quota" role="alert" class="alert-warn mt-3">
              Database is over its {{ dbLimitMb }} MB cap and is read-only. Delete rows or drop tables to free space; writes come back within a minute.
            </p>
            <div v-if="app.quota?.storage?.over_quota" role="alert" class="alert-warn mt-3 flex flex-wrap items-center justify-between gap-2">
              <span>Blob storage bucket is over its {{ storageLimitMb }} MB cap and is read-only.</span>
              <button type="button" class="btn-secondary btn-sm" :disabled="suspended" @click="freeOpen = true">Free up space</button>
            </div>
            <p class="help mt-2">Sizes refresh about every minute (database) and every five minutes (blob storage). To shrink a database, TRUNCATE or DROP TABLE; DELETE alone may not reduce the size.</p>
          </section>

          <BackupsCard :app-id="app.id" :suspended="suspended" :has-data="!!(app.want_db || app.want_storage)" />

          <section id="logs" class="card card-pad scroll-mt-4" aria-labelledby="h-logs">
            <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 id="h-logs" class="section-title">Logs <span class="meta font-normal">last 200 lines</span></h2>
              <div class="flex items-center gap-3">
                <label class="flex items-center gap-1.5 text-xs text-zinc-300">
                  <input v-model="autoRefresh" type="checkbox" class="check" /> Auto-refresh
                </label>
                <IconButton :icon="RefreshCw" :label="logsBusy ? 'Loading logs…' : 'Refresh logs'" :busy="logsBusy" @click="loadLogs" />
                <IconButton :icon="Download" label="Download logs" :busy="dlBusy" @click="downloadLogs" />
              </div>
            </div>
            <p v-if="logsError" class="text-sm text-zinc-400">{{ logsError }}</p>
            <template v-else-if="logs">
              <div v-if="(logs.pods?.length ?? 0) > 1" class="mb-1 flex flex-wrap items-center gap-2 text-xs text-zinc-300">
                <label for="log-pod">Pod</label>
                <select id="log-pod" v-model="podSel" class="input mono w-auto min-w-0 max-w-full" @change="loadLogs">
                  <option value="">All pods ({{ logs.pods!.length }})</option>
                  <option v-for="p in logs.pods" :key="p" :value="p">{{ podLabel(p) }}</option>
                </select>
                <IconButton :icon="Download" :label="eventsPod ? 'Download pod events' : 'Download pod events (select a pod first)'" :disabled="!eventsPod" :busy="evBusy" @click="downloadEvents" />
              </div>
              <div v-else-if="logs.pod" class="mb-1 flex items-center gap-2">
                <p class="meta mono truncate" :title="logs.pod">Pod {{ podLabel(logs.pod) }}</p>
                <IconButton :icon="Download" label="Download pod events" :busy="evBusy" @click="downloadEvents" />
              </div>
              <pre
                v-if="logs.lines.length"
                ref="logBox"
                tabindex="0"
                aria-label="Application logs"
                class="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-md border border-pico-border bg-black/40 p-3 mono leading-relaxed text-zinc-200"
              >{{ logText }}</pre>
              <p v-else class="rounded-md border border-dashed border-pico-border px-3 py-4 text-center text-sm text-zinc-400">
                {{ logs.note || 'No log output yet. Logs appear once your app is running.' }}
              </p>
            </template>
            <p v-else class="text-sm text-zinc-400">Loading logs…</p>
          </section>

          <section v-if="events.length" class="card card-pad" aria-labelledby="h-events">
            <h2 id="h-events" class="section-title mb-2">Recent events</h2>
            <ul class="space-y-1 text-sm">
              <li v-for="ev in events" :key="ev.at + ev.kind" class="flex flex-col gap-0.5 sm:flex-row sm:gap-3">
                <span class="meta mono shrink-0" :title="fullTime(ev.at) + ' (' + relTime(ev.at, now) + ')'">{{ stamp(parseTs(ev.at).toISOString()) }}</span>
                <span class="min-w-0 text-zinc-200">{{ ev.message }}</span>
              </li>
            </ul>
          </section>

          <section v-if="versions.length" class="card card-pad" aria-labelledby="h-versions">
            <h2 id="h-versions" class="section-title mb-1">Recent versions</h2>
            <ul class="divide-y divide-pico-border text-sm">
              <li v-for="v in versions" :key="v.digest" class="flex min-h-8 items-center gap-3 py-1">
                <span class="mono">{{ shortDigest(v.digest) }}</span>
                <span class="meta" :title="fullTime(v.created_at)">{{ relTime(v.created_at, now) }}</span>
                <span v-if="v.current" class="badge-ok ml-auto">current</span>
                <IconButton v-else :icon="Undo2" sm class="ml-auto" label="Roll back to this version" :disabled="suspended || rollingBack" @click="rollbackTarget = v" />
              </li>
            </ul>
          </section>

          <section class="card card-pad" aria-labelledby="h-conf">
            <h2 id="h-conf" class="section-title mb-3">Configuration</h2>
            <dl class="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2 [&>div]:min-w-0 [&_dd]:wrap-break-word">
              <div class="min-w-0">
                <dt class="meta">Image</dt>
                <dd class="mono truncate text-zinc-200" :title="app.image">{{ app.image }}</dd>
                <dd v-if="app.image_digest && !isDigestRef" class="meta">
                  <span class="mono" :title="app.image_digest">{{ shortDigest(app.image_digest) }}</span>
                  <template v-if="app.image_checked_at"> · checked <span :title="fullTime(app.image_checked_at)">{{ relTime(app.image_checked_at, now) }}</span></template>
                  <template v-if="app.auto_update === false"> · auto-update off</template>
                </dd>
              </div>
              <div><dt class="meta">Container port</dt><dd class="mono text-zinc-200">{{ app.port }}</dd></div>
              <div>
                <dt class="meta">Resources</dt>
                <dd class="mono text-zinc-200">
                  {{ res.cpu }}m CPU · {{ res.mem }} MB RAM
                  <span v-if="res.custom" class="badge-info ml-1">custom</span>
                </dd>
              </div>
              <div><dt class="meta">Health check</dt><dd class="text-zinc-200"><span v-if="app.health_check_enabled" class="mono">{{ app.health_check_path }}</span><template v-else>Off</template></dd></div>
              <div>
                <dt class="meta">Database</dt>
                <dd class="text-zinc-200">{{ app.want_db ? `Attached (${dbEngineLabel(app.db_engine)}, ${limits?.db_mb ?? 50} MB)` : 'Not enabled' }}</dd>
              </div>
              <div>
                <dt class="meta">Blob storage</dt>
                <dd class="text-zinc-200">{{ app.want_storage ? `Attached (${limits?.bucket_mb ?? 100} MB, S3-compatible)` : 'Not enabled' }}</dd>
              </div>
              <div v-if="app.volume">
                <dt class="meta">Persistent volume</dt>
                <dd class="mono text-zinc-200">
                  {{ app.volume.size_gi }} GiB at {{ app.volume.mount_path }}
                  <span v-if="app.volume.phase" :class="[app.volume.phase === 'Bound' ? 'badge-ok' : 'badge-warn', 'ml-1']">{{ app.volume.phase }}</span>
                </dd>
              </div>
              <div><dt class="meta">Environment variables</dt><dd class="mono text-zinc-200">{{ app.env.length }}<span v-if="secretCount" class="text-zinc-400"> ({{ secretCount }} secret)</span></dd></div>
              <div><dt class="meta">Created</dt><dd class="mono text-zinc-200" :title="fullTime(app.created_at)">{{ relTime(app.created_at, now) }}</dd></div>
            </dl>

            <details v-if="hasAddons" class="mt-4 rounded-md border border-pico-border bg-pico-card px-3 py-2 text-sm">
              <summary class="cursor-pointer text-zinc-200">Connection details</summary>
              <div class="mt-2 space-y-3 text-xs text-zinc-300">
                <p>
                  Credentials are injected through env var references and are never shown here. Add a variable whose value is a reference,
                  e.g. <code class="mono">DATABASE_URL = {{ refLabel('db.url') }}</code>.
                </p>
                <div v-if="usedRefs.length">
                  <p class="mb-1 font-medium text-zinc-200">Used by this app</p>
                  <ul class="mono space-y-0.5">
                    <li v-for="u in usedRefs" :key="u.key + u.ref" class="break-all">{{ u.key }} <span class="text-zinc-400">=</span> {{ refLabel(u.ref) }}</li>
                  </ul>
                </div>
                <p v-else class="text-amber-300">No env var references them yet, so your app can't see the credentials. <RouterLink :to="`/apps/${app.id}/edit`" class="link">Add one</RouterLink>.</p>
                <div v-if="app.want_db">
                  <p class="mb-1 font-medium text-zinc-200">Database references</p>
                  <p class="mono break-all">{{ DB_REFS.map(refLabel).join('  ') }}</p>
                </div>
                <div v-if="app.want_storage">
                  <p class="mb-1 font-medium text-zinc-200">Blob storage references</p>
                  <p class="mono break-all">{{ STORAGE_REFS.map(refLabel).join('  ') }}</p>
                </div>
              </div>
            </details>
          </section>

        </div>
      </template>
    </main>

    <ConfirmDialog
      :open="rollbackTarget !== null"
      title="Roll back to this version"
      :message="`Redeploy ${shortDigest(rollbackTarget?.digest)} and stop updating from the tag until a newer image is pushed. The app restarts.`"
      confirm-label="Roll back"
      busy-label="Rolling back…"
      :busy="rollingBack"
      @confirm="confirmRollback"
      @cancel="rollbackTarget = null"
    />
    <FreeStorageDialog :open="freeOpen" :busy="freeBusy" @confirm="confirmFree" @cancel="freeOpen = false" />
  </div>
</template>
