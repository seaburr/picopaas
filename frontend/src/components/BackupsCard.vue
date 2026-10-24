<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { backUpNow, getBackupExport, getBackups, setBackupConfig, startBackupDownload } from '../api/backups'
import { ApiError } from '../api/client'
import { fullTime, relTime, parseTs, useNow } from '../composables/appUi'
import { useToastStore } from '../stores/toast'
import type { BackupExport, BackupRun, BackupsInfo } from '../types'

// Backups card for the App health page. Hidden when the platform has backups off / unconfigured / nothing to back up.
const props = defineProps<{ appId: number; suspended: boolean; hasData: boolean }>()
const toast = useToastStore()
const now = useNow()

const info = ref<BackupsInfo | null>(null)
const loadError = ref('')
const busy = ref(false)
const runBusy = ref(false)
// run id -> export being prepared (or last failed state)
const exportsByRun = ref<Record<number, BackupExport>>({})
const timers = new Map<number, ReturnType<typeof setTimeout>>()
let runsPoll: ReturnType<typeof setInterval> | null = null
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

async function load(quiet = false) {
  try {
    info.value = await getBackups(props.appId)
    loadError.value = ''
  } catch (e) {
    if (!quiet) loadError.value = e instanceof ApiError ? e.message : "Couldn't load backups."
  }
}

// Refresh while a backup is queued/running.
watch(inFlight, (on) => {
  if (on && !runsPoll) runsPoll = setInterval(() => load(true), 5000)
  else if (!on && runsPoll) {
    clearInterval(runsPoll)
    runsPoll = null
  }
})

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

function openUrl(url: string, filename: string | null) {
  const a = document.createElement('a')
  a.href = url
  if (filename) a.download = filename
  a.rel = 'noopener noreferrer'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

function stopPoll(runId: number) {
  const t = timers.get(runId)
  if (t) clearTimeout(t)
  timers.delete(runId)
}

function pollExport(runId: number, exportId: number) {
  const t = setTimeout(async () => {
    timers.delete(runId)
    if (gone) return
    try {
      const ex = await getBackupExport(props.appId, exportId)
      if (gone) return
      if (ex.status === 'ready' && ex.url) {
        delete exportsByRun.value[runId]
        toast.success(`Download ready${ex.size_bytes != null ? ` (${fmtBytes(ex.size_bytes)})` : ''}. The link works for 10 minutes.`)
        openUrl(ex.url, ex.filename)
      } else if (ex.status === 'failed' || ex.status === 'expired') {
        exportsByRun.value[runId] = ex
        toast.error(ex.status === 'expired' ? 'That download expired. Start it again.' : `The download failed: ${ex.error ?? 'try again.'}`)
      } else {
        exportsByRun.value[runId] = ex
        pollExport(runId, exportId)
      }
    } catch (e) {
      delete exportsByRun.value[runId]
      toast.error(errMsg(e, "Couldn't check the download."))
    }
  }, 3000)
  timers.set(runId, t)
}

async function download(run: BackupRun) {
  if (exportsByRun.value[run.id] && ['queued', 'running'].includes(exportsByRun.value[run.id].status)) return
  try {
    const ex = await startBackupDownload(props.appId, run.id)
    exportsByRun.value[run.id] = ex
    stopPoll(run.id)
    pollExport(run.id, ex.export_id)
  } catch (e) {
    const s = e instanceof ApiError ? e.status : 0
    const m = errMsg(e, "Couldn't start the download.")
    if (s === 409) toast.info(m || 'Another download is being prepared. Wait for it to finish.')
    else if (s === 429) toast.error(m || 'You have used the 3 downloads per hour. Try again later.')
    else if (s === 410) toast.error(m || 'That backup is no longer available.')
    else if (s === 503) toast.error(m || "Downloads aren't available right now. Try again in a minute.")
    else toast.error(m)
  }
}

const exportBusy = (run: BackupRun) => {
  const s = exportsByRun.value[run.id]?.status
  return s === 'queued' || s === 'running'
}
const anyExportBusy = computed(() => Object.values(exportsByRun.value).some((x) => x.status === 'queued' || x.status === 'running'))

const statusClass = (s: string) => (s === 'succeeded' ? 'badge-ok' : s === 'failed' ? 'badge-error' : 'badge-neutral')
const sizeOf = (r: BackupRun) => fmtBytes(r.bytes_added)
const when = (iso: string | null) => (iso && !isNaN(parseTs(iso).getTime()) ? relTime(iso, now.value) : '–')

onMounted(() => load())
onBeforeUnmount(() => {
  gone = true
  for (const t of timers.values()) clearTimeout(t)
  timers.clear()
  if (runsPoll) clearInterval(runsPoll)
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
            Last: {{ when(k.last_backup_at) }}<template v-if="k.last_bytes != null"> · {{ fmtBytes(k.last_bytes) }}</template>
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
            <span class="meta" :title="fullTime(r.created_at)">{{ when(r.created_at) }} · {{ r.trigger }}</span>
            <span v-if="r.status === 'succeeded'" class="meta">{{ sizeOf(r) }}</span>
            <span v-if="r.error" class="min-w-0 basis-full truncate text-xs text-danger-light" :title="r.error">{{ r.error }}</span>
            <span class="ml-auto flex items-center gap-2">
              <span v-if="exportsByRun[r.id] && exportBusy(r)" class="meta" role="status">Preparing download ({{ exportsByRun[r.id].status }})…</span>
              <span v-else-if="exportsByRun[r.id]?.status === 'failed'" class="text-xs text-danger-light">Download failed</span>
              <button
                v-if="r.status === 'succeeded' && r.kind !== 'volume'"
                type="button"
                class="btn-secondary btn-sm"
                :disabled="anyExportBusy"
                @click="download(r)"
              >{{ exportBusy(r) ? 'Preparing…' : 'Download' }}</button>
            </span>
          </li>
        </ul>
      </div>
      <p v-else class="meta mt-3">No backups yet.</p>
    </template>
  </section>
</template>
