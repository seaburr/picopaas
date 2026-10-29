<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { getBackupHealth, runPlatformDbBackup } from '../api/admin'
import { ApiError } from '../api/client'
import { stamp, stampTitle } from '../utils/format'
import { parseTs } from '../composables/appUi'
import { fmt } from '../utils/tz'
import IconButton from './IconButton.vue'
import { useToastStore } from '../stores/toast'
import { DatabaseBackup } from 'lucide-vue-next'
import AdminAppBackups from './AdminAppBackups.vue'
import type { BackupFailure } from '../types'

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

const MB = 1048576
const GB = 1024 * MB
const size = (n: number) => (n >= GB ? `${(n / GB).toFixed(2)} GB` : n >= MB ? `${(n / MB).toFixed(1)} MB` : n > 0 ? `${Math.max(1, Math.round(n / 1024))} KB` : '0')
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
  <section id="sec-backups" class="scroll-mt-4" aria-label="Backups">
    <h2 class="mb-1.5 text-sm font-semibold text-zinc-300">Backups</h2>
    <div v-if="error" role="alert" class="alert-error">{{ error }}</div>
    <p v-else-if="!data" class="text-sm text-zinc-400" role="status">Loading backup health…</p>
    <div v-else class="space-y-3">
      <p v-if="!data.enabled || !data.configured" class="alert-info" role="status">
        {{ !data.configured ? 'Backups are not configured on this platform.' : 'Backups are switched off.' }}
      </p>

      <div v-if="pdb" class="min-w-0 rounded-md border border-pico-border bg-pico-panel p-3" :class="pdb.stale ? 'border-amber-500/40' : ''">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span class="text-xs text-zinc-400">
            Platform database backup
            <span v-if="pdb.stale" class="ml-1 rounded-sm bg-amber-500/20 px-1.5 py-0.5 text-amber-400">stale</span>
          </span>
          <IconButton :icon="DatabaseBackup" :label="running ? 'Queueing…' : 'Back up now'" :disabled="running || !pdb.active" @click="runNow" />
        </div>
        <p v-if="!pdb.enabled || !pdb.active" class="mt-1 text-xs text-zinc-400" role="status">{{ !pdb.enabled ? 'Platform database backups are switched off.' : 'Platform database backups are not active.' }}</p>
        <p class="mt-1 text-xs text-zinc-400">
          Every {{ pdb.interval_hours }}h · keep {{ pdb.keep.daily }}d/{{ pdb.keep.weekly }}w/{{ pdb.keep.monthly }}m ·
          last success <span class="mono" :title="pdb.last_success_at ? stampTitle(pdb.last_success_at) : ''">{{ pdb.last_success_at ? stamp(pdb.last_success_at) : 'never' }}</span>
          <template v-if="pdb.repo_bytes"> · repo {{ size(pdb.repo_bytes) }}</template>
        </p>
        <p v-if="lastRun" class="mt-1 text-xs" :class="lastRun.status === 'failed' ? 'text-danger-light' : 'text-zinc-400'">
          Last run: {{ lastRun.status }} ({{ lastRun.trigger }}) <span class="mono" :title="stampTitle(lastRun.finished_at ?? lastRun.created_at)">{{ stamp(lastRun.finished_at ?? lastRun.created_at) }}</span><template v-if="lastRun.error">: {{ lastRun.error }}</template>
        </p>
        <p v-else class="mt-1 text-xs text-zinc-400">No runs yet.</p>
      </div>

      <div class="min-w-0 rounded-md border border-pico-border bg-pico-panel p-3" :class="st?.warning ? 'border-amber-500/40' : ''">
        <div class="mb-1 flex flex-wrap items-baseline justify-between gap-x-3 text-xs text-zinc-400">
          <span>Backup storage</span>
          <span class="mono">{{ size(st!.used_bytes) }} of {{ size(st!.cap_bytes) }}<template v-if="st!.used_pct !== null"> ({{ st!.used_pct }}%)</template> · {{ st!.apps_with_backups }} {{ st!.apps_with_backups === 1 ? 'app' : 'apps' }}</span>
        </div>
        <div
          class="relative h-3 overflow-hidden rounded-sm bg-pico-card" role="img"
          :aria-label="`Backup storage ${size(st!.used_bytes)} of ${size(st!.cap_bytes)}`"
        >
          <div :class="['absolute inset-y-0 left-0', hot ? 'bg-amber-500' : 'bg-accent']" :style="{ width: pct + '%' }" />
        </div>
        <p v-if="st!.over_cap" class="mt-2 text-xs text-danger-light" role="alert">Platform backup storage is over its cap: new backups are refused until space is freed.</p>
        <p v-if="st!.warning" class="mt-2 text-xs text-amber-400" role="status">{{ st!.warning }}</p>
        <p class="mt-1 text-xs text-zinc-400">{{ st!.note }}</p>
        <div v-if="st!.top_apps.length" class="mt-2 overflow-x-auto">
          <table class="tbl min-w-176">
            <thead>
              <tr><th scope="col">Largest repositories</th><th scope="col">Owner</th><th scope="col" class="text-right">Total</th><th scope="col" class="text-right">Ceiling</th><th scope="col">State</th><th scope="col" class="text-right">DB</th><th scope="col" class="text-right">Volume</th><th scope="col" class="text-right">Bucket</th><th scope="col"><span class="sr-only">Backups</span></th></tr>
            </thead>
            <tbody class="text-zinc-300">
              <template v-for="a in st!.top_apps" :key="a.app_id"><tr>
                <td class="wrap-break-word"><span v-if="a.deleted" class="text-zinc-400" title="Backups of deleted apps are kept 14 days so they can still be restored, then purged automatically.">{{ a.name }} <span class="text-zinc-500">deleted<template v-if="a.purge_after"> · purged after {{ purgeDate(a.purge_after) }}</template></span></span><RouterLink v-else :to="`/apps/${a.app_id}`" class="link">{{ a.name }}</RouterLink></td>
                <td class="break-all" :title="a.owner_email ?? undefined">{{ a.owner_email ?? '-' }}</td>
                <td class="mono text-right">{{ size(a.repo_bytes) }}</td>
                <td class="mono text-right">{{ a.cap_bytes != null ? size(a.cap_bytes) : '-' }}</td>
                <td><span v-if="a.full" class="badge-error">full</span> <span v-if="a.admin_disabled" class="badge-warn" title="Backups locked off by an administrator">locked</span></td>
                <td class="mono text-right">{{ size(a.db_bytes) }}</td>
                <td class="mono text-right">{{ size(a.volume_bytes) }}</td>
                <td class="mono text-right">{{ size(a.bucket_bytes) }}</td>
                <td class="text-right"><button type="button" class="btn-secondary btn-sm" :aria-expanded="openApp === a.app_id" @click="openApp = openApp === a.app_id ? null : a.app_id">{{ openApp === a.app_id ? 'Hide' : 'Backups' }}</button></td>
              </tr>
              <tr v-if="openApp === a.app_id"><td colspan="9"><AdminAppBackups :app-id="a.app_id" /></td></tr></template>
            </tbody>
          </table>
        </div>
      </div>

      <div v-if="extraDeleted.length" class="min-w-0 rounded-md border border-pico-border bg-pico-panel p-3">
        <h3 class="mb-1 text-xs font-semibold text-zinc-300">Deleted apps with retained backups</h3>
        <ul class="divide-y divide-pico-border text-xs text-zinc-300">
          <li v-for="a in extraDeleted" :key="a.app_id" class="py-1.5">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <span class="min-w-0 wrap-break-word">{{ a.name }} <span class="text-zinc-500">{{ a.owner_email ?? '' }}<template v-if="a.purge_after"> · purged after {{ purgeDate(a.purge_after) }}</template> · {{ size(a.repo_bytes) }}</span></span>
              <button type="button" class="btn-secondary btn-sm" :aria-expanded="openApp === a.app_id" @click="openApp = openApp === a.app_id ? null : a.app_id">{{ openApp === a.app_id ? 'Hide' : 'Backups' }}</button>
            </div>
            <div v-if="openApp === a.app_id" class="mt-2"><AdminAppBackups :app-id="a.app_id" /></div>
          </li>
        </ul>
      </div>

      <div>
        <h3 class="mb-1 text-xs font-semibold text-zinc-300">Failed backups, last {{ data.days }} days</h3>
        <div class="card overflow-x-auto">
          <table v-if="data.failures.length" class="tbl min-w-240">
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
                <td><span :class="f.still_failing ? 'badge-error' : 'badge-neutral'">{{ f.still_failing ? 'still failing' : 'recovered' }}</span></td>
                <td class="max-w-88 whitespace-pre-wrap wrap-break-word text-xs leading-4">{{ f.error || 'No reason recorded.' }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="px-3 py-4 text-center text-sm text-zinc-400">No failed backups in the last {{ data.days }} days</p>
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
        <h3 class="mb-1 text-xs font-semibold text-amber-300">Stuck ({{ data.stuck.length }})</h3>
        <div class="card overflow-x-auto border-amber-500/40">
          <table class="tbl min-w-160">
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
  </section>
</template>
