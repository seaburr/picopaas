<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Download, Trash2 } from 'lucide-vue-next'
import ConfirmDialog from './ConfirmDialog.vue'
import IconButton from './IconButton.vue'
import { backUpNow, deleteBackupRun, getBackupExport, getBackups, startBackupDownload } from '../api/backups'
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
const runBusy = ref(false)
// run ids whose prepare request is awaiting a response; export ids being fetched for a fresh URL
const preparing = ref<Set<number>>(new Set())
const fetching = ref<Set<number>>(new Set())
// run ids the user asked to download on this page: download starts by itself when the export turns ready
const wantDownload = new Set<number>()
const delTarget = ref<BackupRun | null>(null)
const delBusy = ref(false)
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
const inFlight = computed(() => (info.value?.runs ?? []).some((r) => r.status === 'queued' || r.status === 'running' || r.status === 'deleting'))
const live = (x: BackupRunExport | null | undefined) => !!x && (x.status === 'queued' || x.status === 'running')
const exportInFlight = computed(() => (info.value?.runs ?? []).some((r) => live(r.export)))

async function load(quiet = false) {
  const before = new Map((info.value?.runs ?? []).map((r) => [r.id, r.export?.status]))
  try {
    const next = await getBackups(props.appId)
    if (gone) return
    info.value = next
    loadError.value = ''
    // An export we saw preparing is now ready: download it if the user asked on this page, else just say so.
    for (const r of next.runs) {
      const prev = before.get(r.id)
      if ((prev === 'queued' || prev === 'running') && r.export?.status === 'ready') {
        if (wantDownload.delete(r.id)) void download(r)
        else toast.success(`${kindLabel(r.kind)} download is ready. It stays available for about an hour.`)
      } else if (wantDownload.has(r.id) && r.export?.status === 'failed') wantDownload.delete(r.id)
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
  wantDownload.add(run.id)
  try {
    await startBackupDownload(props.appId, run.id)
    await load(true)
  } catch (e) {
    wantDownload.delete(run.id)
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

function onDownload(run: BackupRun) {
  if (exState(run) === 'ready') void download(run)
  else void prepare(run)
}

async function confirmDelete() {
  const run = delTarget.value
  if (!run || delBusy.value) return
  delBusy.value = true
  try {
    await deleteBackupRun(props.appId, run.id)
    toast.success('Deleting the backup.')
  } catch (e) {
    const s = e instanceof ApiError ? e.status : 0
    if (s === 404) toast.info('That backup is already gone.')
    else toast.error(errMsg(e, "Couldn't delete the backup."))
  } finally {
    delBusy.value = false
    delTarget.value = null
    await load(true)
  }
}

const exState = (r: BackupRun) => {
  const s = r.export?.status
  return !s || s === 'expired' ? 'none' : s === 'queued' || s === 'running' ? 'busy' : s
}
const nextAt = computed(() => (info.value?.next_backup_at ? clock(info.value.next_backup_at) : ''))
const clock = (iso: string) => parseTs(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
const triggerLabel = (t: string) => (t === 'manual' ? 'manual' : t === 'scheduled' || t === 'schedule' ? 'automatic' : t)

const statusClass = (s: string) => (s === 'succeeded' ? 'badge-ok' : s === 'failed' ? 'badge-error' : 'badge-neutral')
const delMessage = computed(() => (delTarget.value ? `Delete this ${kindLabel(delTarget.value.kind).toLowerCase()} backup from ${clock(delTarget.value.created_at)}? It can't be undone.` : ''))
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
  <section v-if="loadError || visible" class="card card-pad min-w-0" aria-labelledby="h-backups">
    <h2 id="h-backups" class="section-title mb-2">Backups</h2>
    <p v-if="loadError" role="alert" class="alert-warn">{{ loadError }}</p>
    <template v-else-if="info">
      <div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <p class="min-w-0 text-sm">
          <span class="font-medium">Scheduled backups:</span>
          {{ info.enabled && info.schedule !== 'off' ? info.schedule : 'off' }}<template v-if="info.enabled && info.schedule !== 'off' && nextAt"> · next {{ nextAt }}</template>
        </p>
        <div class="flex shrink-0 flex-wrap items-center gap-2">
          <RouterLink :to="`/apps/${appId}/edit#backups`" class="btn-secondary btn-sm">Backup settings</RouterLink>
          <button type="button" class="btn-secondary btn-sm" :disabled="runBusy || inFlight || suspended" @click="runNow">
            {{ runBusy ? 'Starting…' : inFlight ? 'Backup running…' : 'Back up now' }}
          </button>
        </div>
      </div>
      <p class="help mt-1">Scheduled backups are off unless you turn them on. "Back up now" works either way (one per hour).</p>
      <p v-if="suspended" class="help mt-1">This app is suspended, so backups can't be started or deleted. You can still download existing backups.</p>

      <div v-if="info.kinds.length" class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div v-for="k in info.kinds" :key="k.kind" class="min-w-0 rounded-md border border-pico-border p-3 text-sm">
          <div class="font-medium">{{ kindLabel(k.kind) }}</div>
          <div v-if="k.last_backup_at" class="meta" :title="fullTime(k.last_backup_at)">
            Last: {{ when(k.last_backup_at) }}<template v-if="k.last_bytes != null && k.last_status !== 'failed'"> · {{ fmtBytes(k.last_bytes) }}</template>
            <span :class="[statusClass(k.last_status === 'ok' ? 'succeeded' : k.last_status ?? ''), 'ml-1']">{{ k.last_status }}</span>
          </div>
          <div v-else class="meta">No backup yet.</div>
          <p v-if="k.last_error" class="mt-1 break-words text-xs text-danger-light">{{ k.last_error }}</p>
        </div>
      </div>

      <div v-if="info.runs.length" class="mt-3">
        <h3 class="table-th mb-1">Recent runs</h3>
        <ul class="divide-y divide-pico-border text-sm">
          <li v-for="r in info.runs" :key="r.id" class="min-w-0 py-1.5">
            <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span class="font-medium">{{ kindLabel(r.kind) }}</span>
              <span :class="statusClass(r.status)">{{ r.status }}</span>
              <span class="badge-neutral" :title="r.trigger === 'manual' ? 'Started by you' : 'Started by the schedule; it can be downloaded too'">{{ triggerLabel(r.trigger) }}</span>
            </div>
            <div class="mt-1 flex min-w-0 items-center justify-between gap-2">
              <span class="meta min-w-0" :title="fullTime(r.created_at)">{{ when(r.created_at) }}<template v-if="r.status === 'succeeded'"> · {{ sizeOf(r) }}</template></span>
              <span v-if="r.status === 'succeeded' || r.status === 'deleting'" class="flex min-w-0 items-center justify-end gap-1.5">
                <template v-if="r.kind !== 'volume' && r.status === 'succeeded'">
                <span v-if="exState(r) === 'busy'" class="meta text-right" role="status">preparing, you can leave this page</span>
                <span v-else-if="exState(r) === 'ready' && r.export?.available_until" class="meta text-right" :title="fullTime(r.export.available_until)">ready until {{ clock(r.export.available_until) }}</span>
                <span v-else-if="exState(r) === 'failed'" class="min-w-0 break-words text-right text-xs text-danger-light">Download failed{{ r.export?.error ? `: ${r.export.error}` : '' }}. Click to retry.</span>
                <button
                  v-if="exState(r) === 'busy' || preparing.has(r.id)"
                  type="button"
                  class="btn-icon"
                  disabled
                  aria-label="Preparing download…"
                  title="Preparing download…"
                >
                  <span class="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" aria-hidden="true"></span>
                </button>
                <IconButton
                  v-else
                  :icon="Download"
                  label="Download backup"
                  :disabled="(r.export ? fetching.has(r.export.id) : false) || delBusy"
                  @click="onDownload(r)"
                />
                </template>
                <IconButton :icon="Trash2" label="Delete backup" danger :disabled="suspended || delBusy || r.status === 'deleting'" @click="delTarget = r" />
              </span>
            </div>
            <p v-if="r.error" class="mt-1 break-words text-xs text-danger-light">{{ r.error }}</p>
          </li>
        </ul>
      </div>
      <p v-else class="meta mt-3">No backups yet.</p>
    </template>

    <ConfirmDialog
      :open="delTarget !== null"
      title="Delete backup"
      :message="delMessage"
      confirm-label="Delete backup"
      busy-label="Deleting…"
      danger
      :busy="delBusy"
      @confirm="confirmDelete"
      @cancel="delTarget = null"
    />
  </section>
</template>
