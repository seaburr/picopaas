<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppStatusNote from '../components/AppStatusNote.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DeleteAppDialog from '../components/DeleteAppDialog.vue'
import FreeStorageDialog from '../components/FreeStorageDialog.vue'
import UsageBar from '../components/UsageBar.vue'
import HostLink from '../components/HostLink.vue'
import { Download, PackageSearch, RefreshCw, RotateCw } from 'lucide-vue-next'
import IconButton from '../components/IconButton.vue'
import NavBar from '../components/NavBar.vue'
import Sparkline from '../components/Sparkline.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { deleteStorageObjects, getAppEvents, getAppLogs, getImageHistory, getMetricsHistory, rotateCredentials, type AppLogs } from '../api/apps'
import { ApiError } from '../api/client'
import { stamp } from '../utils/format'
import { eventNote, fullTime, parseTs, relTime, useNow } from '../composables/appUi'
import { refLabel, refsIn } from '../composables/envKeys'
import { useAppsStore } from '../stores/apps'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'
import { DB_REFS, STORAGE_REFS, type App, type AppEvent, type ImageVersion, type MetricsHistory } from '../types'

const props = defineProps<{ id: string }>()
const router = useRouter()
const route = useRoute()
const store = useAppsStore()
const auth = useAuthStore()
const toast = useToastStore()
const now = useNow()

const app = ref<App | null>(null)
const loadError = ref('')
const notFound = ref(false)

async function loadApp(quiet = false) {
  try {
    app.value = await store.fetchOne(Number(props.id))
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
const autoRefresh = ref(false)
const dlBusy = ref(false)
// eslint-disable-next-line no-control-regex
const ANSI_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]/g

async function downloadLogs() {
  const a = app.value
  if (!a || dlBusy.value) return
  dlBusy.value = true
  try {
    const r = await getAppLogs(a.id, 1000)
    const lines = r.lines.map((l) => l.replace(ANSI_RE, ''))
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

const logBox = ref<HTMLElement | null>(null)
let logTimer: ReturnType<typeof setInterval> | null = null

async function loadLogs() {
  logsBusy.value = true
  try {
    logs.value = await getAppLogs(Number(props.id), 200)
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

// Credentials
const rotating = ref<'db' | 'storage' | null>(null)
const rotateBusy = ref(false)
async function confirmRotate() {
  const target = rotating.value
  if (!target || !app.value) return
  rotateBusy.value = true
  try {
    await rotateCredentials(app.value.id, target)
    toast.success(target === 'db' ? 'Database password rotated. Restarting the app to pick it up.' : 'Storage keys rotated. Restarting the app to pick them up.')
    rotating.value = null
    await loadApp(true)
  } catch (e) {
    toast.error(`Couldn't rotate: ${e instanceof ApiError ? e.message : 'unknown error'}`)
    rotating.value = null
  } finally {
    rotateBusy.value = false
  }
}

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
        ? `Deleted ${r.deleted} objects, but the bucket is still over its cap (${left} MB).`
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

// Delete
const deleteOpen = ref(false)
const deleting = ref(false)
async function confirmDelete() {
  if (!app.value) return
  deleting.value = true
  const name = app.value.name
  try {
    await store.remove(app.value.id)
    toast.success(`Deleted ${name}.`)
    await router.push('/apps')
  } catch (e) {
    toast.error(`Couldn't delete ${name}: ${e instanceof ApiError ? e.message : 'unknown error'}`)
    deleteOpen.value = false
  } finally {
    deleting.value = false
  }
}

const limits = computed(() => auth.user?.limits)
const res = computed(() => {
  const r = app.value?.resources
  return {
    cpu: r?.cpu_limit_m ?? limits.value?.cpu_limit_m ?? 100,
    mem: r?.mem_limit_mi ?? limits.value?.mem_limit_mi ?? 384,
    custom: !!r?.overridden,
  }
})
const hasAddons = computed(() => !!app.value && (app.value.want_db || app.value.want_storage))
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

    <main id="main" tabindex="-1" class="mx-auto max-w-4xl px-4 py-4 sm:px-6">
      <div v-if="notFound" class="card card-pad text-center">
        <p class="section-title">App not found</p>
        <p class="help">It may have been deleted.</p>
        <RouterLink to="/apps" class="btn-secondary mt-4">Back to apps</RouterLink>
      </div>

      <div v-else-if="loadError && !app" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
        <span>{{ loadError }}</span>
        <button type="button" class="btn-secondary btn-sm" @click="loadApp()">Retry</button>
      </div>

      <div v-else-if="!app" class="space-y-3" aria-busy="true" aria-label="Loading app">
        <div class="h-6 w-48 animate-pulse rounded bg-pico-panel" />
        <div class="h-32 animate-pulse rounded bg-pico-panel" />
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
            </div>
            <HostLink :hostname="app.hostname" class="mt-1" />
          </div>
          <div class="flex items-center gap-2">
            <IconButton v-if="!isDigestRef" :icon="PackageSearch" :label="suspended ? 'Check for new image (suspended)' : checking ? 'Checking…' : 'Check for new image'" :disabled="suspended || checking" @click="checkImage" />
            <IconButton :icon="RotateCw" :label="suspended ? 'Restart (suspended: contact the administrator)' : restarting ? 'Restarting…' : 'Restart'" :disabled="suspended || restarting" @click="restart" />
            <RouterLink v-if="!suspended" :to="`/apps/${app.id}/edit`" class="btn-primary">Edit</RouterLink>
            <button v-else type="button" class="btn-primary" disabled title="Suspended. Contact the administrator to restore.">Edit</button>
          </div>
        </header>

        <div class="space-y-3">
          <section class="card card-pad" aria-labelledby="h-status">
            <h2 id="h-status" class="section-title mb-2">Status</h2>
            <p v-if="app.status === 'running'" class="text-sm text-zinc-200">
              Running and serving traffic.
              <span class="text-zinc-400">Created <span :title="fullTime(app.created_at)">{{ relTime(app.created_at, now) }}</span>.</span>
            </p>
            <AppStatusNote :app="app" :restarting="restarting" @restart="restart" />
            <p v-if="app.image_update_error" role="alert" class="alert-warn mt-2">{{ app.image_update_error }}</p>
            <p v-if="app.last_event" class="alert-info mt-2">{{ eventNote(app.last_event, now) }}</p>
          </section>

          <section v-if="versions.length" class="card card-pad" aria-labelledby="h-versions">
            <h2 id="h-versions" class="section-title mb-2">Recent versions</h2>
            <ul class="space-y-1 text-sm">
              <li v-for="v in versions" :key="v.digest" class="flex items-center gap-3">
                <span class="mono">{{ shortDigest(v.digest) }}</span>
                <span class="meta" :title="fullTime(v.created_at)">{{ relTime(v.created_at, now) }}</span>
                <span v-if="v.current" class="meta">current</span>
                <button v-else type="button" class="btn-secondary ml-auto" :disabled="suspended || rollingBack" @click="rollbackTarget = v">Roll back</button>
              </li>
            </ul>
          </section>

          <section v-if="metrics && !suspended" class="card card-pad" aria-labelledby="h-metrics">
            <h2 id="h-metrics" class="section-title mb-2">Resource usage</h2>
            <p v-if="!metrics.points.length" class="meta">Collecting&hellip; the first points show up within a couple of minutes.</p>
            <div v-else class="grid gap-x-6 gap-y-3 sm:grid-cols-2">
              <Sparkline label="CPU" unit="m" :values="metrics.points.map((p) => p.cpu_m)" :limit="metrics.cpu_limit_m" />
              <Sparkline label="Mem" unit="Mi" :values="metrics.points.map((p) => p.mem_mi)" :limit="metrics.mem_limit_mi" />
            </div>
          </section>

          <section v-if="events.length" class="card card-pad" aria-labelledby="h-events">
            <h2 id="h-events" class="section-title mb-2">Recent events</h2>
            <ul class="space-y-1 text-sm">
              <li v-for="ev in events" :key="ev.at + ev.kind" class="flex gap-3">
                <span class="meta mono shrink-0" :title="fullTime(ev.at) + ' (' + relTime(ev.at, now) + ')'">{{ stamp(parseTs(ev.at).toISOString()) }}</span>
                <span class="text-zinc-200">{{ ev.message }}</span>
              </li>
            </ul>
          </section>

          <section v-if="app.quota?.db || app.quota?.storage" class="card card-pad" aria-labelledby="h-usage">
            <h2 id="h-usage" class="section-title mb-2">Usage</h2>
            <div class="grid gap-x-6 gap-y-3 sm:grid-cols-2">
              <UsageBar v-if="app.quota?.db" label="Database" :usage="app.quota.db" />
              <UsageBar v-if="app.quota?.storage" label="Storage" :usage="app.quota.storage" />
            </div>
            <p v-if="app.quota?.db?.over_quota" role="alert" class="alert-warn mt-3">
              Database is over its {{ dbLimitMb }} MB cap and is read-only. Delete rows or drop tables to free space; writes come back within a minute.
            </p>
            <div v-if="app.quota?.storage?.over_quota" role="alert" class="alert-warn mt-3 flex flex-wrap items-center justify-between gap-2">
              <span>Storage bucket is over its {{ storageLimitMb }} MB cap and is read-only.</span>
              <button type="button" class="btn-secondary btn-sm" :disabled="suspended" @click="freeOpen = true">Free up space</button>
            </div>
            <p class="help mt-2">Sizes refresh about every minute (database) and every five minutes (storage). To shrink a database, TRUNCATE or DROP TABLE; DELETE alone may not reduce the size.</p>
          </section>

          <section id="logs" class="card card-pad scroll-mt-4" aria-labelledby="h-logs">
            <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 id="h-logs" class="section-title">Logs <span class="meta font-normal">last 200 lines</span></h2>
              <div class="flex items-center gap-3">
                <label class="flex items-center gap-1.5 text-xs text-zinc-300">
                  <input v-model="autoRefresh" type="checkbox" class="check" /> Auto-refresh
                </label>
                <IconButton :icon="RefreshCw" :label="logsBusy ? 'Loading logs…' : 'Refresh logs'" :disabled="logsBusy" @click="loadLogs" />
                <IconButton :icon="Download" label="Download logs" :disabled="dlBusy" @click="downloadLogs" />
              </div>
            </div>
            <p v-if="logsError" class="text-sm text-zinc-400">{{ logsError }}</p>
            <template v-else-if="logs">
              <p v-if="logs.pod" class="meta mono mb-1 truncate" :title="logs.pod">Pod {{ logs.pod }}</p>
              <pre
                v-if="logs.lines.length"
                ref="logBox"
                tabindex="0"
                aria-label="Application logs"
                class="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-md border border-pico-border bg-black/40 p-3 mono leading-relaxed text-zinc-200"
              >{{ logs.lines.join('\n') }}</pre>
              <p v-else class="rounded-md border border-dashed border-pico-border px-3 py-4 text-center text-sm text-zinc-400">
                {{ logs.note || 'No log output yet. Logs appear once your app is running.' }}
              </p>
            </template>
            <p v-else class="text-sm text-zinc-400">Loading logs…</p>
          </section>

          <section class="card card-pad" aria-labelledby="h-conf">
            <h2 id="h-conf" class="section-title mb-3">Configuration</h2>
            <dl class="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
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
                  {{ res.cpu }}m CPU · {{ res.mem }}Mi RAM
                  <span v-if="res.custom" class="badge-info ml-1">custom</span>
                </dd>
              </div>
              <div><dt class="meta">Health check</dt><dd class="text-zinc-200"><span v-if="app.health_check_enabled" class="mono">{{ app.health_check_path }}</span><template v-else>Off</template></dd></div>
              <div>
                <dt class="meta">Database</dt>
                <dd class="text-zinc-200">{{ app.want_db ? `Attached (MySQL, ${limits?.db_mb ?? 50} MB)` : 'Not enabled' }}</dd>
              </div>
              <div>
                <dt class="meta">Storage</dt>
                <dd class="text-zinc-200">{{ app.want_storage ? `Attached (${limits?.bucket_mb ?? 100} MB, S3-compatible)` : 'Not enabled' }}</dd>
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
                  <p class="mb-1 font-medium text-zinc-200">Storage references</p>
                  <p class="mono break-all">{{ STORAGE_REFS.map(refLabel).join('  ') }}</p>
                </div>
              </div>
            </details>
          </section>

          <section v-if="hasAddons" class="card card-pad" aria-labelledby="h-cred">
            <h2 id="h-cred" class="section-title">Credentials</h2>
            <p class="help">
              Rotating generates new credentials and the old ones stop working immediately. The app restarts to pick up the new ones, so expect a short
              interruption.
            </p>
            <div class="mt-3 flex flex-wrap gap-2">
              <button v-if="app.want_db" type="button" class="btn-secondary" :disabled="suspended" @click="rotating = 'db'">Rotate database password</button>
              <button v-if="app.want_storage" type="button" class="btn-secondary" :disabled="suspended" @click="rotating = 'storage'">Rotate storage keys</button>
            </div>
          </section>

          <section class="card card-pad border-danger/40" aria-labelledby="h-danger">
            <h2 id="h-danger" class="section-title text-danger-light">Danger zone</h2>
            <div class="mt-2 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <p class="text-sm text-zinc-300">
                Delete this app{{ hasAddons ? ', its database and storage bucket' : '' }}. This can't be undone.
              </p>
              <button type="button" class="btn-danger shrink-0" @click="deleteOpen = true">Delete app…</button>
            </div>
          </section>
        </div>
      </template>
    </main>

    <ConfirmDialog
      :open="rotating !== null"
      :title="rotating === 'db' ? 'Rotate database password' : 'Rotate storage keys'"
      message="New credentials are generated and the old ones stop working. The app restarts to pick them up."
      confirm-label="Rotate"
      busy-label="Rotating…"
      danger
      :busy="rotateBusy"
      @confirm="confirmRotate"
      @cancel="rotating = null"
    />
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
    <DeleteAppDialog :app="deleteOpen ? app : null" :busy="deleting" @confirm="confirmDelete" @cancel="deleteOpen = false" />
  </div>
</template>
