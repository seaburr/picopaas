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
      <label class="flex items-center gap-2 text-sm text-zinc-200">
        <input type="checkbox" role="switch" class="check" :checked="info.enabled" :disabled="busy || suspended" @change="toggle" />
        Scheduled backups
      </label>
      <div>
        <label for="bk-schedule" class="label">Schedule</label>
        <select id="bk-schedule" class="input !w-40" :value="schedule" :disabled="busy || suspended || !info.enabled" @change="onSchedule">
          <option value="daily">Daily</option>
          <option value="weekly">Weekly</option>
        </select>
        <p v-if="info.enabled && nextAt" class="help">Next backup: {{ nextAt }}</p>
      </div>
      <p class="help">Keeps the latest daily, weekly and monthly backup.</p>
    </div>
  </section>
</template>
