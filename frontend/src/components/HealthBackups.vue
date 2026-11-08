<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { getBackupHealth, runPlatformDbBackup } from '../api/admin'
import { ApiError } from '../api/client'
import { formatBytes, stamp, stampTitle } from '../utils/format'
import { parseTs } from '../composables/appUi'
import { fmt } from '../utils/tz'
import IconButton from './IconButton.vue'
import { useToastStore } from '../stores/toast'
import PlatformBackupDownloadDialog from './PlatformBackupDownloadDialog.vue'
import AdminAppBackups from './AdminAppBackups.vue'
import type { BackupFailure, PlatformBackupRun } from '../types'

const toast = useToastStore()
const running = ref(false)
async function runNow() {
  running.value = true
  try {
    await runPlatformDbBackup()
    toast.success('Platform database backup queued.')
    await load()
  } catch (e) {
    toast.error(e instanceof ApiError ? e.message : "Couldn't queue the backup.")
  } finally {
    running.value = false
  }
}
const pdb = computed(() => data.value?.platform_db ?? null)
const lastRun = computed(() => pdb.value?.runs?.[0] ?? null)
const openApp = ref<number | null>(null)
// Download dialog: a snapshot of the run taken on click, so the 60s refresh never resets an open dialog.
const dlRun = ref<PlatformBackupRun | null>(null)
const dismissedFail = ref<number | null>(null)
const failedDl = computed(() => (pdb.value?.failed_download && pdb.value.failed_download.id !== dismissedFail.value ? pdb.value.failed_download : null))
const exportLabel = (r: PlatformBackupRun) => {
  const s = r.export?.status
  return s === 'queued' || s === 'running' ? 'preparing download' : s === 'ready' ? 'download ready' : ''
}

const data = ref<Awaited<ReturnType<typeof getBackupHealth>> | null>(null)
const error = ref('')
let timer: number | undefined
async function load() {
  try {
    data.value = await getBackupHealth(7)
    error.value = ''
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load backup health."
  }
}
onMounted(() => {
  load()
  timer = window.setInterval(load, 60000)
})
onBeforeUnmount(() => window.clearInterval(timer))

const size = formatBytes
const st = computed(() => data.value?.storage)
const pct = computed(() => Math.min(100, st.value?.used_pct ?? 0))
// Deleted apps with retained backups that fell outside the top-N table above.
const extraDeleted = computed(() => {
  const shown = new Set(st.value?.top_apps.map((a) => a.app_id))
  return (st.value?.deleted_apps ?? []).filter((a) => !shown.has(a.app_id))
})
const hot = computed(() => (st.value?.used_pct ?? 0) >= 80)
const dur = (s: number) => (s >= 3600 ? `${Math.floor(s / 3600)}h` : `${Math.max(1, Math.floor(s / 60))}m`)
defineExpose({ load })
const sweep = computed(() => data.value?.sweeper ?? null)
const cleanedText = computed(() => {
  const c = Object.entries(sweep.value?.cleaned ?? {})
  return c.length ? c.map(([k, v]) => `${v} ${k.replace(/_/g, ' ')}`).join(', ') : 'nothing to clean'
})
const purgeDate = (iso: string) => fmt(parseTs(iso), { month: 'short', day: 'numeric' })
const label = (f: BackupFailure) => f.app_name ?? `app ${f.app_id}`
</script>

<template>
  <section id="sec-backups" class="sec scroll-mt-4 pt-3" aria-label="Backups">
    <h2 class="section-title mb-2">Backups</h2>
    <div v-if="error" role="alert" class="alert-error">{{ error }}</div>
    <p v-else-if="!data" class="text-sm text-zinc-400" role="status">Loading backup health…</p>
    <div v-else class="space-y-4">
      <p v-if="!data.enabled || !data.configured" class="alert-info" role="status">
        {{ !data.configured ? 'Backups are not configured on this platform.' : 'Backups are switched off.' }}
      </p>

      <div v-if="pdb" class="min-w-0">
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h3 class="text-sm font-medium text-white">Platform database backup</h3>
          <span v-if="pdb.stale" class="st tone-warn text-xs"><span class="dot dot-warn" aria-hidden="true" />stale</span>
          <span class="flex-1" />
          <IconButton :label="running ? 'Queueing…' : 'Back up now'" text="Back up now" :disabled="running || !pdb.active" @click="runNow" />
        </div>
        <p v-if="!pdb.enabled || !pdb.active" class="meta" role="status">{{ !pdb.enabled ? 'Platform database backups are switched off.' : 'Platform database backups are not active.' }}</p>
        <p class="meta">
          Every {{ pdb.interval_hours }}h · keep {{ pdb.keep.daily }}d/{{ pdb.keep.weekly }}w/{{ pdb.keep.monthly }}m ·
          last success <span class="mono" :title="pdb.last_success_at ? stampTitle(pdb.last_success_at) : ''">{{ pdb.last_success_at ? stamp(pdb.last_success_at) : 'never' }}</span>
          <template v-if="pdb.repo_bytes"> · repo <span class="mono">{{ size(pdb.repo_bytes) }}</span></template>
        </p>
        <p v-if="lastRun" class="text-xs" :class="lastRun.status === 'failed' ? 'text-danger-light' : 'text-zinc-400'">
          Last run: {{ lastRun.status }} ({{ lastRun.trigger }}) <span class="mono" :title="stampTitle(lastRun.finished_at ?? lastRun.created_at)">{{ stamp(lastRun.finished_at ?? lastRun.created_at) }}</span><template v-if="lastRun.error">: {{ lastRun.error }}</template>
        </p>
        <p v-else class="meta">No runs yet.</p>
        <div v-if="failedDl" class="alert-warn mt-2 flex items-start justify-between gap-2 text-xs" role="status">
          <span class="min-w-0 wrap-break-word">A download of backup {{ failedDl.snapshot_id ?? '' }} failed<template v-if="failedDl.failed_at"> at {{ stamp(failedDl.failed_at) }}</template><template v-if="failedDl.error">: {{ failedDl.error }}</template></span>
          <IconButton label="Dismiss" text="Dismiss" sm @click="dismissedFail = failedDl.id" />
        </div>
        <div v-if="pdb.runs.length" class="mt-2 overflow-x-auto">
          <table class="tbl min-w-[34rem]" aria-label="Recent platform backups">
            <colgroup><col class="w-44" /><col class="w-28" /><col class="w-24" /><col class="w-24" /><col /><col class="w-28" /></colgroup>
            <thead><tr><th scope="col">When</th><th scope="col">Status</th><th scope="col">Trigger</th><th scope="col" class="text-right">Added</th><th scope="col">Export</th><th scope="col"><span class="sr-only">Action</span></th></tr></thead>
            <tbody class="text-zinc-300">
              <tr v-for="r in pdb.runs" :key="r.id">
                <td class="mono" :title="stampTitle(r.finished_at ?? r.created_at)">{{ stamp(r.finished_at ?? r.created_at) }}</td>
                <td><span class="st" :class="r.status === 'failed' ? 'tone-err' : r.status === 'succeeded' || r.status === 'ok' ? 'tone-ok' : 'tone-off'"><span class="dot" :class="r.status === 'failed' ? 'dot-err' : r.status === 'succeeded' || r.status === 'ok' ? 'dot-ok' : 'dot-off'" aria-hidden="true" />{{ r.status }}</span></td>
                <td>{{ r.trigger }}</td>
                <td class="mono text-right">{{ r.bytes_added ? '+' + size(r.bytes_added) : '-' }}</td>
                <td class="text-zinc-400">{{ exportLabel(r) }}</td>
                <td><div class="row-acts"><button v-if="r.downloadable" type="button" class="row-act" :aria-label="`Download backup from ${stamp(r.finished_at ?? r.created_at)}`" @click="dlRun = r">Download</button></div></td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="pdb.download_per_hour" class="meta mt-1">Up to {{ pdb.download_per_hour }} {{ pdb.download_per_hour === 1 ? 'download' : 'downloads' }} per hour.</p>
      </div>

      <div class="min-w-0 border-t border-pico-border pt-2">
        <div class="mb-1 flex flex-wrap items-baseline justify-between gap-x-3 text-xs text-zinc-400">
          <h3 class="text-sm font-medium text-white">Backup storage</h3>
          <span class="mono">{{ size(st!.used_bytes) }} of {{ size(st!.cap_bytes) }}<template v-if="st!.used_pct !== null"> ({{ st!.used_pct }}%)</template> · {{ st!.apps_with_backups }} {{ st!.apps_with_backups === 1 ? 'app' : 'apps' }}</span>
        </div>
        <div
          class="relative h-2 overflow-hidden rounded-sm bg-pico-card" role="img"
          :aria-label="`Backup storage ${size(st!.used_bytes)} of ${size(st!.cap_bytes)}`"
        >
          <div :class="['absolute inset-y-0 left-0', hot ? 'bg-amber-500' : 'bg-accent']" :style="{ width: pct + '%' }" />
        </div>
        <p v-if="st!.over_cap" class="mt-2 text-xs text-danger-light" role="alert">Platform backup storage is over its cap: new backups are refused until space is freed.</p>
        <p v-if="st!.warning" class="mt-2 text-xs text-amber-400" role="status">{{ st!.warning }}</p>
        <p class="meta mt-1">{{ st!.note }}</p>
        <div v-if="st!.top_apps.length" class="mt-2 overflow-x-auto">
          <table class="tbl min-w-[56rem]">
            <colgroup><col class="w-44" /><col class="w-48" /><col class="w-20" /><col class="w-20" /><col class="w-28" /><col class="w-20" /><col class="w-20" /><col class="w-20" /><col class="w-24" /></colgroup>
            <thead>
              <tr><th scope="col">Largest repositories</th><th scope="col">Owner</th><th scope="col" class="text-right">Total</th><th scope="col" class="text-right">Ceiling</th><th scope="col">State</th><th scope="col" class="text-right">DB</th><th scope="col" class="text-right">Volume</th><th scope="col" class="text-right">Bucket</th><th scope="col"><span class="sr-only">Backups</span></th></tr>
            </thead>
            <tbody class="text-zinc-300">
              <template v-for="a in st!.top_apps" :key="a.app_id"><tr>
                <td class="wrap-break-word"><span v-if="a.deleted" class="text-zinc-400" title="Backups of deleted apps are kept 14 days so they can still be restored, then purged automatically.">{{ a.name }} <span class="text-zinc-500">deleted<template v-if="a.purge_after"> · purged after {{ purgeDate(a.purge_after) }}</template></span></span><RouterLink v-else :to="`/apps/${a.app_id}`" class="link">{{ a.name }}</RouterLink></td>
                <td class="truncate" :title="a.owner_email ?? undefined">{{ a.owner_email ?? '-' }}</td>
                <td class="mono text-right">{{ size(a.repo_bytes) }}</td>
                <td class="mono text-right">{{ a.cap_bytes != null ? size(a.cap_bytes) : '-' }}</td>
                <td><span v-if="a.full" class="st tone-err"><span class="dot dot-err" aria-hidden="true" />full</span> <span v-if="a.admin_disabled" class="st tone-warn" title="Backups locked off by an administrator"><span class="dot dot-warn" aria-hidden="true" />locked</span></td>
                <td class="mono text-right">{{ size(a.db_bytes) }}</td>
                <td class="mono text-right">{{ size(a.volume_bytes) }}</td>
                <td class="mono text-right">{{ size(a.bucket_bytes) }}</td>
                <td><div class="row-acts"><button type="button" class="row-act" :aria-expanded="openApp === a.app_id" @click="openApp = openApp === a.app_id ? null : a.app_id">{{ openApp === a.app_id ? 'Hide' : 'Backups' }}</button></div></td>
              </tr>
              <tr v-if="openApp === a.app_id"><td colspan="9"><AdminAppBackups :app-id="a.app_id" /></td></tr></template>
            </tbody>
          </table>
        </div>
      </div>

      <div v-if="extraDeleted.length" class="min-w-0">
        <h3 class="mb-1 text-sm font-medium text-white">Deleted apps with retained backups</h3>
        <div class="overflow-x-auto">
          <table class="tbl min-w-[36rem]">
            <colgroup><col /><col class="w-48" /><col class="w-36" /><col class="w-24" /><col class="w-24" /></colgroup>
            <thead><tr><th scope="col">App</th><th scope="col">Owner</th><th scope="col">Purged after</th><th scope="col" class="text-right">Size</th><th scope="col"><span class="sr-only">Backups</span></th></tr></thead>
            <tbody class="text-zinc-300">
              <template v-for="a in extraDeleted" :key="a.app_id">
                <tr>
                  <td class="wrap-break-word">{{ a.name }}</td>
                  <td class="truncate" :title="a.owner_email ?? undefined">{{ a.owner_email ?? '-' }}</td>
                  <td class="mono">{{ a.purge_after ? purgeDate(a.purge_after) : '-' }}</td>
                  <td class="mono text-right">{{ size(a.repo_bytes) }}</td>
                  <td><div class="row-acts"><button type="button" class="row-act" :aria-expanded="openApp === a.app_id" @click="openApp = openApp === a.app_id ? null : a.app_id">{{ openApp === a.app_id ? 'Hide' : 'Backups' }}</button></div></td>
                </tr>
                <tr v-if="openApp === a.app_id"><td colspan="5"><AdminAppBackups :app-id="a.app_id" /></td></tr>
              </template>
            </tbody>
          </table>
        </div>
      </div>

      <div>
        <h3 class="mb-1 text-sm font-medium text-white">Failed backups, last {{ data.days }} days</h3>
        <div class="overflow-x-auto">
          <table v-if="data.failures.length" class="tbl min-w-[56rem]">
            <colgroup><col class="w-40" /><col class="w-44" /><col class="w-20" /><col class="w-24" /><col class="w-40" /><col class="w-32" /><col /></colgroup>
            <thead>
              <tr><th scope="col">App</th><th scope="col">Owner</th><th scope="col">Kind</th><th scope="col">Trigger</th><th scope="col">When</th><th scope="col">Status</th><th scope="col">Why</th></tr>
            </thead>
            <tbody class="text-zinc-300">
              <tr v-for="f in data.failures" :key="f.run_id" class="align-top">
                <td class="wrap-break-word"><span v-if="f.app_deleted" class="text-zinc-400">{{ label(f) }} (deleted)</span><RouterLink v-else :to="`/apps/${f.app_id}`" class="link">{{ label(f) }}</RouterLink></td>
                <td class="break-all" :title="f.owner_email ?? undefined">{{ f.owner_email ?? '-' }}</td>
                <td>{{ f.kind }}</td>
                <td>{{ f.trigger }}</td>
                <td class="mono whitespace-nowrap" :title="f.at ? stampTitle(f.at) : undefined">{{ f.at ? stamp(f.at) : '-' }}</td>
                <td><span class="st" :class="f.still_failing ? 'tone-err' : 'tone-off'"><span class="dot" :class="f.still_failing ? 'dot-err' : 'dot-off'" aria-hidden="true" />{{ f.still_failing ? 'still failing' : 'recovered' }}</span></td>
                <td class="whitespace-pre-wrap wrap-break-word text-xs leading-4">{{ f.error || 'No reason recorded.' }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="py-2 text-sm text-zinc-400">No failed backups in the last {{ data.days }} days</p>
        </div>
      </div>

      <p v-if="sweep" class="text-xs text-zinc-400" role="status">
        Last cleanup sweep:
        <template v-if="sweep.last_run_at"><span class="mono" :title="stampTitle(sweep.last_run_at)">{{ stamp(sweep.last_run_at) }}</span>, {{ cleanedText }}</template>
        <template v-else>has not run yet</template>.
        <span v-if="sweep.errors.length" class="text-amber-300">Errors: {{ sweep.errors.join('; ') }}.</span>
        <span v-if="sweep.orphan_repos.length" class="text-amber-300">Orphan repositories (not removed): {{ sweep.orphan_repos.join(', ') }}.</span>
      </p>

      <div v-if="data.stuck.length">
        <h3 class="mb-1 text-sm font-medium text-amber-300">Stuck ({{ data.stuck.length }})</h3>
        <div class="overflow-x-auto">
          <table class="tbl min-w-[40rem]">
            <thead><tr><th scope="col">App</th><th scope="col">Owner</th><th scope="col">Kind</th><th scope="col">Trigger</th><th scope="col">State</th><th scope="col" class="text-right">For</th></tr></thead>
            <tbody class="text-zinc-300">
              <tr v-for="f in data.stuck" :key="f.run_id">
                <td class="wrap-break-word"><span v-if="f.app_deleted" class="text-zinc-400">{{ label(f) }} (deleted)</span><RouterLink v-else :to="`/apps/${f.app_id}`" class="link">{{ label(f) }}</RouterLink></td>
                <td class="break-all" :title="f.owner_email ?? undefined">{{ f.owner_email ?? '-' }}</td>
                <td>{{ f.kind }}</td>
                <td>{{ f.trigger }}</td>
                <td>{{ f.status }}</td>
                <td class="mono text-right">{{ f.age_seconds != null ? dur(f.age_seconds) : '-' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
    <PlatformBackupDownloadDialog :run="dlRun" @close="dlRun = null" @changed="load" />
  </section>
</template>
