<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import BackupsCard from '../components/BackupsCard.vue'
import AppNotFound from '../components/AppNotFound.vue'
import AppStatusNote from '../components/AppStatusNote.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import FreeStorageDialog from '../components/FreeStorageDialog.vue'
import UsageGauge from '../components/UsageGauge.vue'
import HostLink from '../components/HostLink.vue'
import IconButton from '../components/IconButton.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { dismissAlert, deleteStorageObjects, getAppEvents, downloadPodEvents, getAppLogs, getImageHistory, getMetricsHistory, type AppLogs } from '../api/apps'
import { ApiError } from '../api/client'
import { stamp } from '../utils/format'
import { uptimeText, dbEngineLabel, eventNote, fullTime, parseTs, primaryHost, servedHosts, relTime, useNow } from '../composables/appUi'
import { refLabel, refsIn } from '../composables/envKeys'
import SuspendedBanner from '../components/SuspendedBanner.vue'
import { useProjectSuspension } from '../composables/projectSuspension'
import { useAppsStore } from '../stores/apps'
import { projectApps, projectLabel } from '../utils/paths'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'
import { DB_REFS, STORAGE_REFS, type App, type AppEvent, type ImageVersion, type MetricsHistory, type UsageStat } from '../types'

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

// Poll while queued / deploying (3s) and while failed (10s): a failed app can recover on its own once its pods are healthy.
let poll: ReturnType<typeof setInterval> | null = null
let pollMs = 0
function syncPoll() {
  const st = app.value?.status
  const want = st === 'pending' || st === 'deploying' ? 3000 : st === 'error' ? 10000 : 0
  if (poll && want !== pollMs) {
    clearInterval(poll)
    poll = null
  }
  if (want && !poll) {
    pollMs = want
    poll = setInterval(() => loadApp(true), want)
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
const limits = computed(() => auth.user?.limits)
const freeOpen = ref(false)
const freeBusy = ref(false)
const toMb = (bytes?: number | null) => (bytes ? Math.round(bytes / 1024 / 1024) : null)
const dbLimitMb = computed(() => toMb(app.value?.quota?.db?.limit_bytes) ?? limits.value?.db_mb ?? null)
const storageLimitMb = computed(() => toMb(app.value?.quota?.storage?.limit_bytes) ?? limits.value?.bucket_mb ?? null)
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


const res = computed(() => {
  const r = app.value?.resources
  return {
    cpu: r?.cpu_limit_m ?? limits.value?.cpu_limit_m ?? '–',
    mem: r?.mem_limit_mi ?? limits.value?.mem_limit_mi ?? '–',
    custom: !!r?.overridden || Object.values(r?.controls ?? {}).some((c) => !!c && c.owner !== null && c.owner < c.max),
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
const { project: appProject, suspended: projSuspended } = useProjectSuspension(() => app.value?.project_id)
const suspended = computed(() => app.value?.status === 'suspended' || projSuspended.value)

// Which env vars reference add-on credentials (non-secret values only; secrets are never readable).
const usedRefs = computed(() =>
  (app.value?.env ?? []).filter((e) => !e.is_secret).flatMap((e) => refsIn(e.value).map((r) => ({ key: e.key, ref: r }))),
)
// Phones get collapsible sections (MobileDetail); from md up every section is open and its summary is just a heading.
const wideQuery = typeof window !== 'undefined' && window.matchMedia ? window.matchMedia('(min-width: 768px)') : null
const wide = ref(wideQuery?.matches ?? true)
const onWide = (e: MediaQueryListEvent) => { wide.value = e.matches }
wideQuery?.addEventListener('change', onWide)
onBeforeUnmount(() => wideQuery?.removeEventListener('change', onWide))
// A section can't stay closed on a wide screen.
const keepOpen = (e: Event) => { const d = e.target as HTMLDetailsElement; if (wide.value && !d.open) d.open = true }

// Rows of the Resource usage table: live CPU / memory from the metrics history, then the add-on usages.
interface MetricRow { name: string; unit: string; used: number; limit: number; pct: number; values: number[]; near: boolean }
const metricRows = computed<MetricRow[]>(() => {
  const m = metrics.value
  if (!m || suspended.value || !m.points.length) return []
  const mk = (name: string, unit: string, values: number[], limit: number): MetricRow => {
    const used = values[values.length - 1] ?? 0
    const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0
    return { name, unit, used, limit, pct, values, near: limit > 0 && used >= limit * 0.8 }
  }
  return [
    mk('CPU', 'm', m.points.map((p) => p.cpu_m), m.cpu_capacity_m ?? m.cpu_limit_m),
    mk('Memory', 'MB', m.points.map((p) => p.mem_mi), m.mem_capacity_mi ?? m.mem_limit_mi),
  ]
})
// Add-on usages (API bytes) as gauge rows; units and limits formatted from the API numbers.
const GB = 1024 ** 3
const MBYTES = 1024 * 1024
function quotaRow(label: string, u: UsageStat) {
  const gb = u.limit_bytes >= GB
  const div = gb ? GB : MBYTES
  const f = (n: number) => (gb ? (n / div).toFixed(2) : (n / div).toFixed(n >= 10 * MBYTES ? 0 : 1))
  return { label, used: f(u.size_bytes), limit: f(u.limit_bytes), unit: gb ? 'GB' : 'MB', pct: u.limit_bytes ? (u.size_bytes / u.limit_bytes) * 100 : 100, over: u.over_quota }
}
const quotaRows = computed(() => {
  const q = app.value?.quota
  return [q?.db && quotaRow('Database', q.db), q?.storage && quotaRow('Blob storage', q.storage), q?.volume && quotaRow('Volume', q.volume)].filter((r) => !!r) as ReturnType<typeof quotaRow>[]
})
const showUsage = computed(() => (!!metrics.value && !suspended.value) || !!(app.value?.quota?.db || app.value?.quota?.storage || app.value?.quota?.volume || app.value?.volume))
const secretCount = computed(() => (app.value?.env ?? []).filter((e) => e.is_secret).length)
</script>

<template>
  <main id="main" tabindex="-1" class="page-main">
    <AppNotFound v-if="notFound" />

    <div v-else-if="loadError && !app" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
      <span>{{ loadError }}</span>
      <IconButton label="Retry" text="Retry" @click="loadApp()" />
    </div>

    <div v-else-if="!app" class="space-y-3" aria-busy="true" aria-label="Loading app">
      <div class="h-6 w-48 animate-pulse rounded-sm bg-pico-muted" />
      <div class="h-32 animate-pulse rounded-sm bg-pico-muted" />
    </div>

    <template v-else>
      <SuspendedBanner :project="appProject" />
      <nav class="meta mb-1" aria-label="Breadcrumb">
        <template v-if="appProject"><RouterLink :to="`/projects/${appProject.id}`" class="link">{{ projectLabel(appProject) }}</RouterLink><span aria-hidden="true"> / </span></template><RouterLink :to="appProject ? projectApps(appProject.id) : '/apps'" class="link">Apps</RouterLink><span aria-hidden="true"> / </span>{{ app.name }}
      </nav>

      <header class="mb-3 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div class="min-w-0">
          <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <h1 class="page-title mono! break-all text-lg!">{{ app.name }}</h1>
            <StatusBadge :status="app.status" />
            <RouterLink v-if="app.kaisen_build_id" :to="`/kaisen/${app.kaisen_build_id}`" class="meta underline" title="Built with Kaisen (Preview)">Built with Kaisen · Preview</RouterLink>
          </div>
          <div class="mt-1 flex min-w-0 flex-wrap items-center gap-x-3">
            <HostLink :hostname="primaryHost(app)" />
            <span v-if="servedHosts(app).length > 1" class="meta">primary</span>
          </div>
          <ul v-if="servedHosts(app).length > 1" class="mt-0.5 min-w-0 text-sm">
            <li v-for="h in servedHosts(app).slice(1)" :key="h" class="min-w-0"><HostLink :hostname="h" /></li>
          </ul>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <IconButton v-if="app.kaisen_build_id" label="Open in Kaisen" text="Open in Kaisen" :to="`/kaisen/${app.kaisen_build_id}`" />
          <IconButton v-if="!isDigestRef" :label="suspended ? 'Check for new image (suspended)' : checking ? 'Checking…' : 'Check for new image'" :disabled="suspended || checking" text="Check for new image" @click="checkImage" />
          <IconButton :label="projSuspended ? 'Restart (project is suspended)' : suspended ? 'Restart (suspended: contact the administrator)' : restarting ? 'Restarting…' : 'Restart'" :disabled="suspended || restarting" text="Restart" @click="restart" />
          <IconButton label="Settings" text="Settings" :to="`/apps/${app.id}/edit`" />
        </div>
      </header>

      <section class="sec sec-pad" aria-labelledby="h-status">
        <h2 id="h-status" class="section-title mb-1 pt-2">Status</h2>
        <p v-if="app.status === 'running'" class="text-sm text-zinc-200">
          Running and serving traffic.
          <span v-if="uptime" class="text-zinc-400" :title="'Since ' + fullTime(new Date(now - uptime * 1000).toISOString())">Uptime <span class="mono">{{ uptimeText(uptime) }}</span>.</span>
        </p>
        <AppStatusNote :app="app" :restarting="restarting" @restart="restart" />
        <p v-if="app.image_update_error" role="alert" class="alert-warn mt-2 flex items-start justify-between gap-2">
          <span>{{ app.image_update_error }}</span>
          <IconButton label="Dismiss alert" text="Dismiss" @click="dismiss('image_update')" />
        </p>
        <p v-if="app.last_event" class="alert-info mt-2 flex items-start justify-between gap-2">
          <span>{{ eventNote(app.last_event, now) }}</span>
          <IconButton label="Dismiss alert" text="Dismiss" @click="dismiss('event')" />
        </p>
      </section>

      <div class="grid gap-x-10 lg:grid-cols-2">
        <section v-if="showUsage" class="sec sec-pad min-w-0" aria-labelledby="h-usage">
          <details :class="wide ? '' : 'disc'" open @toggle="keepOpen">
            <summary class="flex flex-wrap items-baseline gap-x-3 pt-2 [&::-webkit-details-marker]:hidden" :class="wide ? 'list-none' : ''">
              <h2 id="h-usage" class="section-title">Resource usage</h2>
              <span v-if="metrics && !suspended" class="meta">{{ podCount }} pod{{ podCount === 1 ? '' : 's' }}, last few hours</span>
            </summary>
            <ul class="mt-1">
              <UsageGauge
                v-for="m in metricRows"
                :key="m.name"
                :label="m.name"
                :used="String(m.used)"
                :limit="String(m.limit)"
                :unit="m.unit"
                :pct="m.pct"
                :values="m.values"
                :limit-raw="m.limit"
                :raw-unit="m.unit"
              />
              <UsageGauge v-for="q in quotaRows" :key="q.label" v-bind="q" />
              <li v-if="app.volume && !app.quota?.volume" class="border-b border-pico-border py-2.5 text-sm">
                <span class="text-zinc-200">Volume</span> <span class="mono ml-3 text-zinc-400">Measuring…</span>
              </li>
            </ul>
            <p v-if="metrics && !suspended && !metrics.points.length" class="meta mt-2">Collecting&hellip; the first CPU and memory points show up within a couple of minutes.</p>
            <p v-if="app.quota?.db?.over_quota" role="alert" class="alert-warn mt-3">
              Database is over its {{ dbLimitMb !== null ? dbLimitMb + ' MB ' : '' }}cap and is read-only. Delete rows or drop tables to free space; writes come back within a minute.
            </p>
            <div v-if="app.quota?.storage?.over_quota" role="alert" class="alert-warn mt-3 flex flex-wrap items-center justify-between gap-2">
              <span>Blob storage bucket is over its {{ storageLimitMb !== null ? storageLimitMb + ' MB ' : '' }}cap and is read-only.</span>
              <button type="button" class="btn-secondary btn-sm" :disabled="suspended" :title="projSuspended ? 'Project is suspended' : undefined" @click="freeOpen = true">Free up space</button>
            </div>
            <p v-if="app.quota?.db || app.quota?.storage || app.quota?.volume" class="help mt-2">Sizes refresh about every minute (database) and every five minutes (blob storage). To shrink a database, TRUNCATE or DROP TABLE; DELETE alone may not reduce the size.</p>
          </details>
        </section>

        <section class="sec sec-pad min-w-0" aria-labelledby="h-conf">
          <details :class="wide ? '' : 'disc'" open @toggle="keepOpen">
            <summary class="flex flex-wrap items-baseline gap-x-3 pt-2 [&::-webkit-details-marker]:hidden" :class="wide ? 'list-none' : ''">
              <h2 id="h-conf" class="section-title">Configuration</h2>
              <span class="grow" />
              <RouterLink :to="`/apps/${app.id}/edit`" class="meta underline max-md:hidden">Edit</RouterLink>
            </summary>
            <table class="kv mt-1 w-full table-fixed text-sm">
              <colgroup><col class="w-[38%]"><col></colgroup>
              <tbody class="[&_td]:border-b [&_td]:border-pico-border [&_td]:py-1 [&_th]:border-b [&_th]:border-pico-border [&_th]:py-1 [&_th]:pr-3 [&_th]:text-left [&_th]:text-xs [&_th]:font-normal [&_th]:text-zinc-400 [&_td]:wrap-break-word [&_th]:align-top [&_td]:align-top">
                <tr>
                  <th scope="row">Image</th>
                  <td>
                    <span class="mono break-all" :title="app.image">{{ app.image }}</span>
                    <span v-if="app.image_digest && !isDigestRef" class="meta block">
                      <span class="mono" :title="app.image_digest">{{ shortDigest(app.image_digest) }}</span>
                      <template v-if="app.image_checked_at"> · checked <span :title="fullTime(app.image_checked_at)">{{ relTime(app.image_checked_at, now) }}</span></template>
                      <template v-if="app.auto_update === false"> · auto-update off</template>
                    </span>
                  </td>
                </tr>
                <tr><th scope="row">Container port</th><td class="mono">{{ app.port }}</td></tr>
                <tr>
                  <th scope="row">Resource limits</th>
                  <td><span class="mono">{{ res.cpu }}m CPU · {{ res.mem }} MB RAM</span><span v-if="res.custom" class="meta"> (custom)</span></td>
                </tr>
                <tr><th scope="row">Health check</th><td><span v-if="app.health_check_enabled" class="mono">{{ app.health_check_path }}</span><template v-else>Off</template></td></tr>
                <tr>
                  <th scope="row">Database</th>
                  <td>{{ app.want_db ? `${dbEngineLabel(app.db_engine)}${dbLimitMb !== null ? ', ' + dbLimitMb + ' MB' : ''}` : 'Not enabled' }}</td>
                </tr>
                <tr>
                  <th scope="row">Blob storage</th>
                  <td>{{ app.want_storage ? `S3-compatible${storageLimitMb !== null ? ', ' + storageLimitMb + ' MB' : ''}` : 'Not enabled' }}</td>
                </tr>
                <tr v-if="app.volume">
                  <th scope="row">Persistent volume</th>
                  <td>
                    <span class="mono">{{ app.volume.size_gi }} GiB at {{ app.volume.mount_path }}</span>
                    <span v-if="app.volume.phase" :class="['st ml-2', app.volume.phase === 'Bound' ? 'tone-ok' : 'tone-warn']">{{ app.volume.phase }}</span>
                  </td>
                </tr>
                <tr><th scope="row">Environment variables</th><td><span class="mono">{{ app.env.length }}</span><span v-if="secretCount" class="text-zinc-400">, <span class="mono">{{ secretCount }}</span> secret</span></td></tr>
                <tr><th scope="row">Created</th><td class="mono" :title="fullTime(app.created_at)">{{ relTime(app.created_at, now) }}</td></tr>
              </tbody>
            </table>

            <details v-if="hasAddons" class="disc mt-3 text-sm">
              <summary class="text-zinc-200">Connection details</summary>
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
          </details>
        </section>
      </div>

      <BackupsCard :app-id="app.id" :suspended="suspended" :has-data="!!(app.want_db || app.want_storage)" />

      <section id="logs" class="sec sec-pad scroll-mt-4" aria-labelledby="h-logs">
        <details :class="wide ? '' : 'disc'" open @toggle="keepOpen">
          <summary class="flex flex-wrap items-baseline gap-x-3 pt-2 [&::-webkit-details-marker]:hidden" :class="wide ? 'list-none' : ''">
            <h2 id="h-logs" class="section-title">Logs</h2>
            <span class="meta">last 200 lines</span>
          </summary>
          <div class="my-1 flex flex-wrap items-center gap-x-4 gap-y-1">
            <template v-if="!logsError && logs">
              <template v-if="(logs.pods?.length ?? 0) > 1">
                <label for="log-pod" class="meta">Pod</label>
                <select id="log-pod" v-model="podSel" class="input mono w-auto min-w-0 max-w-full" @change="loadLogs">
                  <option value="">All pods ({{ logs.pods!.length }})</option>
                  <option v-for="p in logs.pods" :key="p" :value="p">{{ podLabel(p) }}</option>
                </select>
              </template>
              <p v-else-if="logs.pod" class="meta mono min-w-0 truncate" :title="logs.pod">Pod {{ podLabel(logs.pod) }}</p>
            </template>
            <span class="grow" />
            <label class="flex items-center gap-1.5 text-xs text-zinc-300 max-md:min-h-11">
              <input v-model="autoRefresh" type="checkbox" class="check" /> Auto-refresh
            </label>
            <IconButton label="Refresh logs" :busy="logsBusy" class="max-md:min-h-11!" @click="loadLogs" />
            <IconButton label="Download logs" :busy="dlBusy" class="max-md:min-h-11!" @click="downloadLogs" />
            <IconButton
              v-if="logs && !logsError && ((logs.pods?.length ?? 0) > 1 || logs.pod)"
              :label="eventsPod ? 'Download pod events' : 'Download pod events (select a pod first)'"
              :disabled="(logs.pods?.length ?? 0) > 1 && !eventsPod"
              :busy="evBusy"
              class="max-md:min-h-11!"
              @click="downloadEvents"
            />
          </div>
          <p v-if="logsError" class="text-sm text-zinc-400">{{ logsError }}</p>
          <template v-else-if="logs">
            <pre
              v-if="logs.lines.length"
              ref="logBox"
              tabindex="0"
              aria-label="Application logs"
              class="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-md border border-pico-border bg-pico-inset p-3 mono leading-relaxed text-zinc-200"
            >{{ logText }}</pre>
            <p v-else class="border-y border-pico-border px-3 py-4 text-center text-sm text-zinc-400">
              {{ logs.note || 'No log output yet. Logs appear once your app is running.' }}
            </p>
          </template>
          <p v-else class="text-sm text-zinc-400">Loading logs…</p>
        </details>
      </section>

      <div>
        <section v-if="events.length" class="sec sec-pad min-w-0" aria-labelledby="h-events">
          <details :class="wide ? '' : 'disc'" :open="wide" @toggle="keepOpen">
            <summary class="flex flex-wrap items-baseline gap-x-3 pt-2 [&::-webkit-details-marker]:hidden" :class="wide ? 'list-none' : ''">
              <h2 id="h-events" class="section-title">Recent events</h2>
              <span class="meta mono md:hidden">{{ events.length }}</span>
            </summary>
            <table class="tbl mt-1">
              <colgroup><col class="w-[34%]"><col></colgroup>
              <thead><tr><th>Time</th><th>Event</th></tr></thead>
              <tbody>
                <tr v-for="ev in events" :key="ev.at + ev.kind">
                  <td class="mono" :title="fullTime(ev.at) + ' (' + relTime(ev.at, now) + ')'">{{ stamp(parseTs(ev.at).toISOString()) }}</td>
                  <td class="wrap-break-word whitespace-normal">{{ ev.message }}</td>
                </tr>
              </tbody>
            </table>
          </details>
        </section>
      </div>

      <section v-if="versions.length" class="sec sec-pad min-w-0" aria-labelledby="h-versions">
        <details :class="wide ? '' : 'disc'" :open="wide" @toggle="keepOpen">
          <summary class="flex flex-wrap items-baseline gap-x-3 pt-2 [&::-webkit-details-marker]:hidden" :class="wide ? 'list-none' : ''">
            <h2 id="h-versions" class="section-title">Recent versions</h2>
            <span class="meta mono md:hidden">{{ versions.length }}</span>
          </summary>
          <table class="tbl mt-1">
            <colgroup><col class="w-[34%]"><col><col class="w-[28%]"></colgroup>
            <thead><tr><th>Version</th><th>Deployed</th><th class="text-right"><span class="sr-only">Action</span></th></tr></thead>
            <tbody>
              <tr v-for="v in versions" :key="v.digest">
                <td class="mono">{{ shortDigest(v.digest) }}</td>
                <td class="mono" :title="fullTime(v.created_at)">{{ relTime(v.created_at, now) }}</td>
                <td class="text-right">
                  <span v-if="v.current" class="meta">current</span>
                  <button v-if="!v.current" type="button" class="row-act max-md:min-h-11!" aria-label="Roll back to this version" :disabled="suspended || rollingBack" @click="rollbackTarget = v">Roll back</button>
                </td>
              </tr>
            </tbody>
          </table>
        </details>
      </section>
    </template>

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
  </main>
</template>
