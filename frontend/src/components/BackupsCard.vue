<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { backUpNow, getBackupExport, getBackups, setBackupConfig, startBackupDownload } from '../api/backups'
import { ApiError } from '../api/client'
import { fullTime, relTime, parseTs, useNow } from '../composables/appUi'
import { useToastStore } from '../stores/toast'
import type { BackupRun, BackupRunExport, BackupsInfo } from '../types'

// Backups card for the App health page. Hidden when the platform has backups off / unconfigured / nothing to back up.
const props = defineProps<{ appId: number; suspended: boolean; hasData: boolean }>()
const toast = useToastStore()
const now = useNow()

const info = ref<BackupsInfo | null>(null)
const loadError = ref('')
const busy = ref(false)
const runBusy = ref(false)
// run ids whose prepare request is awaiting a response; export ids being fetched for a fresh URL
const preparing = ref<Set<number>>(new Set())
const fetching = ref<Set<number>>(new Set())
let poll: ReturnType<typeof setInterval> | null = null
let gone = false

const KIND_LABEL: Record<string, string> = { db: 'Database', bucket: 'Blob storage', volume: 'Volume' }
const kindLabel = (k: string) => KIND_LABEL[k] ?? k

function fmtBytes(n: number | null | undefined): string {
  if (n == null) return '–'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(2)} GB`
}

const available = computed(() => !!info.value && info.value.feature_enabled && info.value.configured)
const visible = computed(() => available.value && (props.hasData || (info.value?.kinds.length ?? 0) > 0))
const inFlight = computed(() => (info.value?.runs ?? []).some((r) => r.status === 'queued' || r.status === 'running'))
const live = (x: BackupRunExport | null | undefined) => !!x && (x.status === 'queued' || x.status === 'running')
const exportInFlight = computed(() => (info.value?.runs ?? []).some((r) => live(r.export)))

async function load(quiet = false) {
  const before = new Map((info.value?.runs ?? []).map((r) => [r.id, r.export?.status]))
  try {
    const next = await getBackups(props.appId)
    if (gone) return
    info.value = next
    loadError.value = ''
    // An export we saw preparing is now ready: tell the user, but don't auto-download.
    for (const r of next.runs) {
      const prev = before.get(r.id)
      if ((prev === 'queued' || prev === 'running') && r.export?.status === 'ready') {
        toast.success(`${kindLabel(r.kind)} download is ready. It stays available for about an hour.`)
      }
    }
  } catch (e) {
    if (!quiet) loadError.value = e instanceof ApiError ? e.message : "Couldn't load backups."
  }
}

// Refresh every 4s while a backup run or an export is queued/running.
watch(
  () => inFlight.value || exportInFlight.value,
  (on) => {
    if (on && !poll) poll = setInterval(() => load(true), 4000)
    else if (!on && poll) {
      clearInterval(poll)
      poll = null
    }
  },
)

function errMsg(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback
}

async function toggle() {
  if (!info.value || busy.value) return
  busy.value = true
  try {
    const on = !info.value.enabled
    // Turning on with schedule "off" would do nothing: pick daily.
    info.value = await setBackupConfig(props.appId, on, on && info.value.schedule === 'off' ? 'daily' : undefined)
    toast.success(on ? 'Scheduled backups turned on.' : 'Scheduled backups turned off.')
  } catch (e) {
    toast.error(`Couldn't change scheduled backups: ${errMsg(e, 'try again.')}`)
  } finally {
    busy.value = false
  }
}

async function runNow() {
  runBusy.value = true
  try {
    await backUpNow(props.appId)
    toast.success('Backup queued. It starts within a minute.')
    await load(true)
  } catch (e) {
    // 409 already running, 429 hourly limit: the message says which and when to retry.
    toast.error(errMsg(e, "Couldn't start the backup."))
  } finally {
    runBusy.value = false
  }
}

async function prepare(run: BackupRun) {
  if (preparing.value.has(run.id) || live(run.export)) return
  preparing.value = new Set(preparing.value).add(run.id)
  try {
    await startBackupDownload(props.appId, run.id)
    await load(true)
  } catch (e) {
    const s = e instanceof ApiError ? e.status : 0
    const m = errMsg(e, "Couldn't start the download.")
    if (s === 409) {
      toast.info(m || 'A download is already being prepared.')
      await load(true)
    } else if (s === 429) toast.error(m || 'You have used the 3 downloads per hour. Try again later.')
    else if (s === 410) toast.error(m || 'That backup is no longer available.')
    else if (s === 503) toast.error(m || "Downloads aren't available right now. Try again in a minute.")
    else toast.error(m)
  } finally {
    const n = new Set(preparing.value)
    n.delete(run.id)
    preparing.value = n
  }
}

// Always fetch the single export for a fresh 10-minute URL; never reuse an old one.
async function download(run: BackupRun) {
  const id = run.export?.id
  if (id == null || fetching.value.has(id)) return
  fetching.value = new Set(fetching.value).add(id)
  try {
    const ex = await getBackupExport(props.appId, id)
    if (ex.status === 'ready' && ex.url) openUrl(ex.url, ex.filename)
    else {
      toast.error(ex.status === 'expired' ? 'That download expired. Prepare it again.' : 'That download is not ready.')
      await load(true)
    }
  } catch (e) {
    toast.error(errMsg(e, "Couldn't get the download link."))
    await load(true)
  } finally {
    const n = new Set(fetching.value)
    n.delete(id)
    fetching.value = n
  }
}

function openUrl(url: string, filename: string | null) {
  const a = document.createElement('a')
  a.href = url
  if (filename) a.download = filename
  a.rel = 'noopener noreferrer'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

const exState = (r: BackupRun) => {
  const s = r.export?.status
  return !s || s === 'expired' ? 'none' : s === 'queued' || s === 'running' ? 'busy' : s
}
const clock = (iso: string) => parseTs(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
const triggerLabel = (t: string) => (t === 'manual' ? 'manual' : t === 'scheduled' || t === 'schedule' ? 'automatic' : t)

const statusClass = (s: string) => (s === 'succeeded' ? 'badge-ok' : s === 'failed' ? 'badge-error' : 'badge-neutral')
const sizeOf = (r: BackupRun) => fmtBytes(r.bytes_added)
const when = (iso: string | null) => (iso && !isNaN(parseTs(iso).getTime()) ? relTime(iso, now.value) : '–')

onMounted(() => load())
onBeforeUnmount(() => {
  gone = true
  if (poll) clearInterval(poll)
  poll = null
})
</script>

<template>
  <section v-if="loadError || visible" class="card card-pad" aria-labelledby="h-backups">
    <h2 id="h-backups" class="section-title mb-2">Backups</h2>
    <p v-if="loadError" role="alert" class="alert-warn">{{ loadError }}</p>
    <template v-else-if="info">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <label class="flex items-center gap-2 text-sm">
          <input type="checkbox" role="switch" :checked="info.enabled" :disabled="busy || suspended" :aria-label="'Scheduled backups'" @change="toggle" />
          <span class="font-medium">Scheduled backups</span>
          <span class="meta">{{ info.enabled ? info.schedule : 'off' }}<template v-if="info.enabled && info.next_backup_at"> · next {{ when(info.next_backup_at) }}</template></span>
        </label>
        <button type="button" class="btn-secondary btn-sm" :disabled="runBusy || inFlight || suspended" @click="runNow">
          {{ runBusy ? 'Starting…' : inFlight ? 'Backup running…' : 'Back up now' }}
        </button>
      </div>
      <p class="help mt-1">Backups are off unless you turn them on. "Back up now" works either way (one per hour).</p>
      <p v-if="suspended" class="help mt-1">This app is suspended, so backups can't be started or changed. You can still download existing backups.</p>

      <div v-if="info.kinds.length" class="mt-3 grid gap-3 sm:grid-cols-2">
        <div v-for="k in info.kinds" :key="k.kind" class="rounded-md border border-pico-border p-3 text-sm">
          <div class="font-medium">{{ kindLabel(k.kind) }}</div>
          <div v-if="k.last_backup_at" class="meta" :title="fullTime(k.last_backup_at)">
            Last: {{ when(k.last_backup_at) }}<template v-if="k.last_bytes != null && k.last_status !== 'failed'"> · {{ fmtBytes(k.last_bytes) }}</template>
            <span :class="[statusClass(k.last_status === 'ok' ? 'succeeded' : k.last_status ?? ''), 'ml-1']">{{ k.last_status }}</span>
          </div>
          <div v-else class="meta">No backup yet.</div>
          <p v-if="k.last_error" class="mt-1 text-xs text-danger-light">{{ k.last_error }}</p>
        </div>
      </div>

      <div v-if="info.runs.length" class="mt-3">
        <h3 class="table-th mb-1">Recent runs</h3>
        <ul class="divide-y divide-pico-border text-sm">
          <li v-for="r in info.runs" :key="r.id" class="flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5">
            <span class="w-28 shrink-0">{{ kindLabel(r.kind) }}</span>
            <span :class="statusClass(r.status)">{{ r.status }}</span>
            <span class="meta" :title="fullTime(r.created_at)">{{ when(r.created_at) }}</span>
            <span class="badge-neutral" :title="r.trigger === 'manual' ? 'Started by you' : 'Started by the schedule; it can be downloaded too'">{{ triggerLabel(r.trigger) }}</span>
            <span v-if="r.status === 'succeeded'" class="meta">{{ sizeOf(r) }}</span>
            <span v-if="r.error" class="min-w-0 basis-full truncate text-xs text-danger-light" :title="r.error">{{ r.error }}</span>
            <span v-if="r.status === 'succeeded' && r.kind !== 'volume'" class="ml-auto flex flex-wrap items-center gap-2">
              <template v-if="exState(r) === 'busy'">
                <span class="meta inline-flex items-center gap-1.5" role="status">
                  <span class="inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" aria-hidden="true"></span>
                  Preparing… you can leave this page
                </span>
              </template>
              <template v-else-if="exState(r) === 'ready'">
                <span v-if="r.export?.available_until" class="meta" :title="fullTime(r.export.available_until)">available until {{ clock(r.export.available_until) }} ({{ when(r.export.available_until) }})</span>
                <button type="button" class="btn-secondary btn-sm" :disabled="fetching.has(r.export!.id)" @click="download(r)">Download</button>
              </template>
              <template v-else>
                <span v-if="exState(r) === 'failed'" class="text-xs text-danger-light">Download failed{{ r.export?.error ? `: ${r.export.error}` : '' }}</span>
                <button type="button" class="btn-secondary btn-sm" :disabled="preparing.has(r.id)" @click="prepare(r)">
                  {{ preparing.has(r.id) ? 'Starting…' : exState(r) === 'failed' ? 'Retry' : 'Prepare download' }}
                </button>
              </template>
            </span>
          </li>
        </ul>
      </div>
      <p v-else class="meta mt-3">No backups yet.</p>
    </template>
  </section>
</template>
