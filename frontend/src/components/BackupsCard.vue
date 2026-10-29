<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Download, Settings, Trash2 } from 'lucide-vue-next'
import ConfirmDialog from './ConfirmDialog.vue'
import IconButton from './IconButton.vue'
import { backUpNow, deleteBackupSet, downloadBackupSet, getBackupExport, getBackups } from '../api/backups'
import { ApiError } from '../api/client'
import { fullTime, relTime, parseTs, useNow } from '../composables/appUi'
import { fmt } from '../utils/tz'
import { useToastStore } from '../stores/toast'
import type { BackupRunExport, BackupSet, BackupsInfo } from '../types'

// Backups card for the App health page. One row per backup (a package of db / blob storage / volume parts).
// Hidden when the platform has backups off / unconfigured / nothing to back up.
const props = defineProps<{ appId: number; suspended: boolean; hasData: boolean }>()
const toast = useToastStore()
const now = useNow()

const info = ref<BackupsInfo | null>(null)
const loadError = ref('')
const runBusy = ref(false)
// set ids whose prepare request is awaiting a response; export ids being fetched for a fresh URL
const preparing = ref<Set<number>>(new Set())
const fetching = ref<Set<number>>(new Set())
// set ids the user asked to download on this page: download starts by itself when the export turns ready
const wantDownload = new Set<number>()
const delTarget = ref<BackupSet | null>(null)
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
const visible = computed(() => available.value && (props.hasData || (info.value?.kinds.length ?? 0) > 0 || (info.value?.backups?.length ?? 0) > 0))
const sets = computed(() => info.value?.backups ?? [])
const live = (x: BackupRunExport | null | undefined) => !!x && (x.status === 'queued' || x.status === 'running')
const inFlight = computed(() => sets.value.some((b) => b.status === 'running' || b.status === 'deleting'))
const backupRunning = computed(() => sets.value.some((b) => b.status === 'running'))
const exportInFlight = computed(() => sets.value.some((b) => live(b.export)))

async function load(quiet = false) {
  const before = new Map(sets.value.map((b) => [b.id, b.export?.status]))
  try {
    const next = await getBackups(props.appId)
    if (gone) return
    info.value = next
    loadError.value = ''
    // An export we saw preparing is now ready: download it if the user asked on this page, else just say so.
    for (const b of next.backups ?? []) {
      const prev = before.get(b.id)
      if ((prev === 'queued' || prev === 'running') && b.export?.status === 'ready') {
        if (wantDownload.delete(b.id)) void download(b)
        else toast.success('Your backup download is ready. It stays available for about an hour.')
      } else if ((prev === 'queued' || prev === 'running') && (!b.export || b.export.status === 'failed')) {
        // The backend drops a failed export from `backup.export` (null), so a preparing -> nothing transition is a failure.
        wantDownload.delete(b.id)
        toast.error(b.failed_export ? `Download failed: ${b.failed_export.error || 'unknown error'}` : "Preparing the download failed. Try again; if it keeps failing, contact the administrator.")
      }
    }
  } catch (e) {
    if (!quiet) loadError.value = e instanceof ApiError ? e.message : "Couldn't load backups."
  }
}

// Refresh every 4s while a backup is running/deleting or an export is queued/running.
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
  if (runBusy.value) return
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

async function prepare(b: BackupSet) {
  if (preparing.value.has(b.id) || live(b.export)) return
  preparing.value = new Set(preparing.value).add(b.id)
  wantDownload.add(b.id)
  try {
    await downloadBackupSet(props.appId, b.id)
    await load(true)
  } catch (e) {
    wantDownload.delete(b.id)
    const s = e instanceof ApiError ? e.status : 0
    const m = errMsg(e, "Couldn't start the download.")
    if (s === 409) {
      toast.info(m || 'A download is already being prepared.')
      await load(true)
    } else if (s === 429) toast.error(m || 'You have used the 3 downloads per hour. Try again later.')
    else if (s === 422 || s === 410) toast.error(m || 'That backup is not available for download.')
    else if (s === 503) toast.error(m || "Downloads aren't available right now. Try again in a minute.")
    else toast.error(m)
  } finally {
    const n = new Set(preparing.value)
    n.delete(b.id)
    preparing.value = n
  }
}

// Always fetch the single export for a fresh URL; never reuse an old one.
async function download(b: BackupSet) {
  const id = b.export?.id
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

const exState = (b: BackupSet) => {
  const s = b.export?.status
  if (!b.export && b.failed_export) return 'failed'
  return !s || s === 'expired' ? 'none' : s === 'queued' || s === 'running' ? 'busy' : s
}
function onDownload(b: BackupSet) {
  if (exState(b) === 'ready') void download(b)
  else void prepare(b)
}

async function confirmDelete() {
  const b = delTarget.value
  if (!b || delBusy.value) return
  delBusy.value = true
  try {
    await deleteBackupSet(props.appId, b.id)
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

const clock = (iso: string) => fmt(parseTs(iso), { hour: '2-digit', minute: '2-digit' })
const nextAt = computed(() => (info.value?.next_backup_at ? clock(info.value.next_backup_at) : ''))
const when = (iso: string | null) => (iso && !isNaN(parseTs(iso).getTime()) ? relTime(iso, now.value) : '–')
const stamp = (iso: string) => fmt(parseTs(iso), { dateStyle: 'medium', timeStyle: 'short' })
const triggerLabel = (t: string) => (t === 'manual' ? 'manual' : t === 'final' ? 'before delete' : 'automatic')
const triggerTitle = (t: string) =>
  t === 'manual' ? 'Started by you' : t === 'final' ? 'Taken when the app was deleted' : 'Started by the schedule; it can be downloaded too'
const statusClass = (s: string) =>
  s === 'succeeded' ? 'badge-ok' : s === 'failed' ? 'badge-error' : s === 'partial' ? 'badge-warn' : 'badge-neutral'
const failedParts = (b: BackupSet) => b.parts.filter((p) => p.status === 'failed')
const partsTitle = (b: BackupSet) => failedParts(b).map((p) => `${kindLabel(p.kind)}: ${p.error || 'failed'}`).join('\n')
const partClass = (s: string) => (s === 'failed' ? 'text-danger-light' : s === 'succeeded' ? '' : 'text-zinc-400')
const delMessage = computed(() => {
  const b = delTarget.value
  if (!b) return ''
  const names = b.parts.map((p) => kindLabel(p.kind).toLowerCase()).join(', ')
  return `Delete this backup (${names || 'database, blob storage, volume'}) from ${stamp(b.created_at)}? It can't be undone.`
})

const locked = computed(() => !!info.value?.admin_disabled)
const full = computed(() => !!info.value?.storage_full)
const lockMsg = computed(() => info.value?.admin_disabled_message || 'Backups are turned off by the administrator.')
const noRun = computed(() => (locked.value ? lockMsg.value : full.value ? 'Backup storage for this app is full. Old backups roll off automatically; try again shortly.' : ''))
const MB = 1048576
const sz = (n: number) => (n >= 1024 * MB ? `${(n / 1024 / MB).toFixed(2)} GB` : `${(n / MB).toFixed(1)} MB`)
const failMsg = (b: BackupSet) => `Download failed: ${b.failed_export?.error || b.export?.error || 'unknown error'}. Click to retry.`

const lastSet = computed(() => sets.value.find((b) => b.status !== 'running' && b.status !== 'deleting') ?? sets.value[0])

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
          {{ locked ? 'off (locked by the administrator)' : info.enabled && info.schedule !== 'off' ? info.schedule : 'off' }}<template v-if="!locked && info.enabled && info.schedule !== 'off' && nextAt"> · next {{ nextAt }}</template>
        </p>
        <div class="flex shrink-0 flex-wrap items-center gap-2">
          <IconButton :icon="Settings" label="Backup settings" :to="`/apps/${appId}/edit#backups`" />
          <button type="button" class="btn-secondary btn-sm" :disabled="runBusy || inFlight || suspended || locked || full" :aria-describedby="noRun ? 'bk-run-reason' : undefined" @click="runNow">
            {{ runBusy ? 'Starting…' : backupRunning ? 'Backup running…' : inFlight ? 'Deleting…' : 'Back up now' }}
          </button>
        </div>
      </div>
      <p v-if="locked" id="bk-run-reason" role="status" class="alert-warn mt-2">{{ lockMsg }} Existing backups are kept and can still be downloaded or deleted.</p>
      <p v-else-if="full" id="bk-run-reason" role="status" class="alert-warn mt-2">
        Backup storage for this app is full ({{ sz(info.repo_bytes ?? 0) }} of {{ sz(info.cap_bytes) }}). The oldest backups roll off automatically, which frees space. You can also delete one yourself.
      </p>
      <p v-else class="help mt-1">Scheduled backups are off unless you turn them on. "Back up now" works either way ({{ info.manual_per_hour ?? 1 }} per hour).</p>
      <p v-if="suspended" class="help mt-1">This app is suspended, so backups can't be started or deleted. You can still download existing backups.</p>

      <p v-if="!info.backups" class="meta mt-3">The backup list needs a newer platform version. Reload in a minute.</p>
      <template v-else>
        <p class="mt-3 min-w-0 wrap-break-word text-sm">
          <span class="font-medium">Last backup:</span>{{ ' ' }}
          <template v-if="lastSet">
            <span :title="fullTime(lastSet.created_at)">{{ when(lastSet.created_at) }}</span> ·
            <span :class="statusClass(lastSet.status)">{{ lastSet.status }}</span>
          </template>
          <template v-else> none yet</template>
        </p>
        <p v-if="info.volume_backups?.has_volume && !info.volume_backups.available" class="meta mt-1 wrap-break-word">
          {{ info.volume_backups.note || 'Volume backups are not available yet; ask the admin.' }}
        </p>

        <ul v-if="sets.length" class="mt-3 divide-y divide-pico-border text-sm">
          <li v-for="b in sets" :key="b.id" class="min-w-0 py-2">
            <div class="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              <span class="min-w-0 font-medium" :title="fullTime(b.created_at)">{{ stamp(b.created_at) }}</span>
              <span class="meta">{{ when(b.created_at) }}</span>
              <span class="badge-neutral" :title="triggerTitle(b.trigger)">{{ triggerLabel(b.trigger) }}</span>
              <span :class="statusClass(b.status)" :title="b.status === 'partial' ? partsTitle(b) : undefined">{{ b.status }}</span>
            </div>
            <p v-if="b.status === 'partial' || b.status === 'failed'" class="mt-1 wrap-break-word text-xs text-amber-300">
              <template v-for="p in failedParts(b)" :key="p.run_id">{{ kindLabel(p.kind) }} failed{{ p.error ? `: ${p.error}` : '' }}. </template>
            </p>
            <div class="mt-1 flex min-w-0 flex-wrap items-center justify-between gap-x-2 gap-y-1">
              <span class="meta flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5">
                <template v-for="(p, i) in b.parts" :key="p.run_id">
                  <span v-if="i > 0" aria-hidden="true">·</span>
                  <span :class="['wrap-break-word', partClass(p.status)]" :title="p.error || p.status">
                    {{ kindLabel(p.kind) }}
                    <template v-if="p.status === 'succeeded'">✓<template v-if="p.bytes_added != null"> {{ fmtBytes(p.bytes_added) }}</template></template>
                    <template v-else>({{ p.status }})</template>
                  </span>
                </template>
              </span>
              <span class="flex min-w-0 flex-wrap items-center justify-end gap-1.5">
                <template v-if="b.status === 'succeeded' || b.status === 'partial'">
                  <span v-if="exState(b) === 'busy'" class="meta text-right" role="status">preparing, you can leave this page</span>
                  <span v-else-if="exState(b) === 'ready' && b.export?.available_until" class="meta text-right" :title="fullTime(b.export.available_until)">ready until {{ clock(b.export.available_until) }}</span>
                  <span v-else-if="exState(b) === 'failed'" class="min-w-0 wrap-break-word text-right text-xs text-danger-light">{{ failMsg(b) }}</span>
                </template>
                <IconButton
                  v-if="exState(b) === 'busy' || preparing.has(b.id)"
                  :icon="Download"
                  label="Preparing download…"
                  busy
                />
                <IconButton
                  v-else
                  :icon="Download"
                  label="Download backup"
                  :disabled="b.status === 'running' || b.status === 'deleting' || b.status === 'failed' || (b.export ? fetching.has(b.export.id) : false)"
                  @click="onDownload(b)"
                />
                <IconButton :icon="Trash2" label="Delete backup" danger :disabled="suspended || b.status === 'running' || b.status === 'deleting'" @click="delTarget = b" />
              </span>
            </div>
          </li>
        </ul>
        <p v-else class="meta mt-3">No backups yet.</p>
      </template>
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
