<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { getBackups, setBackupConfig } from '../api/backups'
import { ApiError } from '../api/client'
import { fullTime, parseTs } from '../composables/appUi'
import { useToastStore } from '../stores/toast'
import type { BackupsInfo } from '../types'

// Backup settings for the Settings page ("Other settings"). Hidden when backups are off/unconfigured or there is nothing to back up.
const props = defineProps<{ appId: number; suspended: boolean; hasData: boolean }>()
const toast = useToastStore()
const info = ref<BackupsInfo | null>(null)
const busy = ref(false)

const visible = computed(() => !!info.value && info.value.feature_enabled && info.value.configured && (props.hasData || info.value.kinds.length > 0))
const schedule = computed(() => (info.value && info.value.schedule !== 'off' ? info.value.schedule : 'daily'))
const MB = 1048576
const size = (n: number) => (n >= 1024 * MB ? `${(n / 1024 / MB).toFixed(2)} GB` : n >= MB ? `${(n / MB).toFixed(1)} MB` : n > 0 ? `${Math.max(1, Math.round(n / 1024))} KB` : '0 MB')
const used = computed(() => info.value?.repo_bytes ?? 0)
const pct = computed(() => (info.value && info.value.cap_bytes > 0 ? Math.min(100, (used.value / info.value.cap_bytes) * 100) : 0))
const locked = computed(() => !!info.value?.admin_disabled)
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`
const nextAt = computed(() => {
  const n = info.value?.next_backup_at
  return n && !isNaN(parseTs(n).getTime()) ? fullTime(n) : ''
})

onMounted(async () => {
  try {
    info.value = await getBackups(props.appId)
  } catch {
    info.value = null
  }
  // The section only exists after this fetch, so the router's hash scroll missed it.
  if (visible.value && location.hash === '#backups') {
    await nextTick()
    document.getElementById('backups')?.scrollIntoView({ block: 'start' })
  }
})

async function save(enabled: boolean, sched?: string, msg = 'Backup settings saved.') {
  if (busy.value) return
  busy.value = true
  try {
    info.value = await setBackupConfig(props.appId, enabled, sched)
    toast.success(msg)
  } catch (e) {
    toast.error(`Couldn't change backup settings: ${e instanceof ApiError ? e.message : 'try again.'}`)
  } finally {
    busy.value = false
  }
}
const toggle = () => save(!info.value!.enabled, !info.value!.enabled ? schedule.value : undefined, info.value!.enabled ? 'Scheduled backups turned off.' : 'Scheduled backups turned on.')
const onSchedule = (e: Event) => save(info.value!.enabled, (e.target as HTMLSelectElement).value, 'Backup schedule saved.')
</script>

<template>
  <section v-if="visible && info" id="backups" class="card card-pad scroll-mt-4 min-w-0" aria-labelledby="h-backup-settings">
    <h2 id="h-backup-settings" class="section-title mb-1">Backups</h2>
    <p class="help !mt-0">Saved as soon as you change it. Backups are encrypted and stored outside the cluster.</p>
    <p v-if="suspended" class="help">This app is suspended, so backup settings can't be changed.</p>
    <div class="mt-3 space-y-3">
      <p v-if="locked" role="status" class="alert-warn">{{ info.admin_disabled_message || 'Backups are turned off by the administrator.' }}</p>
      <label class="flex items-center gap-2 text-sm text-zinc-200">
        <input type="checkbox" role="switch" class="check" :checked="info.enabled" :disabled="busy || suspended || locked" @change="toggle" />
        Scheduled backups: {{ info.enabled ? 'on' : 'off' }}
      </label>
      <div>
        <label for="bk-schedule" class="label">Schedule</label>
        <select id="bk-schedule" class="input !w-40" :value="schedule" :disabled="busy || suspended || locked || !info.enabled" @change="onSchedule">
          <option value="daily">Daily</option>
          <option value="weekly">Weekly</option>
        </select>
        <p v-if="info.enabled && nextAt" class="help">Next backup: {{ nextAt }}</p>
      </div>
      <div v-if="info.repo_bytes != null">
        <div class="flex flex-wrap items-baseline justify-between gap-x-3 text-xs text-zinc-400">
          <span>Backup storage<template v-if="info.cap_override"> (ceiling set by an administrator)</template></span>
          <span class="mono">{{ size(used) }} of {{ size(info.cap_bytes) }}</span>
        </div>
        <div class="relative mt-1 h-2 overflow-hidden rounded bg-pico-card" role="img" :aria-label="`Backup storage ${size(used)} of ${size(info.cap_bytes)}`">
          <div :class="['absolute inset-y-0 left-0', info.storage_full ? 'bg-danger' : pct >= 80 ? 'bg-amber-500' : 'bg-accent']" :style="{ width: pct + '%' }" />
        </div>
        <p v-if="info.storage_full" class="mt-1 text-xs text-amber-300">Backup storage is full. New backups are refused until older ones are deleted or roll off.</p>
      </div>
      <p v-if="info.max_held" class="help">
        Keeps the last {{ info.max_held }} backups (oldest failed ones are removed first).
        <template v-if="info.manual_per_hour && info.export_per_hour">Limits: {{ plural(info.manual_per_hour, 'manual backup') }} and {{ plural(info.export_per_hour, 'download') }} per hour.</template>
      </p>
    </div>
  </section>
</template>
