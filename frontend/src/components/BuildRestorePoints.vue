<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import BuildDiffPanel from './BuildDiffPanel.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import IconButton from './IconButton.vue'
import { ApiError } from '../api/client'
import { getSnapshotDiff, listSnapshots, restoreSnapshot } from '../api/kaisen'
import { parseTs, relTime } from '../composables/appUi'
import { fmtSize } from '../composables/kaisen'
import type { KaisenSnapshot } from '../types'

const props = defineProps<{
  buildId: number
  now: number
  active: boolean // a run is active: restoring is blocked
  canRun: boolean
  canDeploy: boolean
  kept?: number // restore points kept per build (build detail snapshots_kept)
  deploying?: boolean
  refreshKey?: string | number
}>()
const emit = defineEmits<{ restored: []; deploy: [] }>()

const items = ref<KaisenSnapshot[] | null>(null)
const error = ref('')
const viewing = ref<number | null>(null)
const target = ref<KaisenSnapshot | null>(null)
const restoring = ref(false)
const restoreError = ref('')
const done = ref('')

const label = (s: KaisenSnapshot) => (s.kind === 'run_start' ? `Before run #${s.run_number ?? s.run_id ?? '?'}` : 'Before restore')
const blocked = computed(() => props.active ? 'A run is active. Restore once it finishes.' : !props.canRun ? "You can't change this build." : '')

async function load() {
  error.value = ''
  try {
    items.value = await listSnapshots(props.buildId)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load restore points."
  }
}
onMounted(load)
watch(() => [props.buildId, props.refreshKey], () => { viewing.value = null; load() })

async function doRestore() {
  if (!target.value) return
  restoring.value = true
  restoreError.value = ''
  done.value = ''
  try {
    const r = await restoreSnapshot(props.buildId, target.value.id)
    done.value = `Restored ${r.files_count} files from ${label(target.value).toLowerCase()}.`
    target.value = null
    viewing.value = null
    emit('restored')
    await load()
  } catch (e) {
    restoreError.value = e instanceof ApiError ? e.message : 'Restore failed. Try again.'
    target.value = null
  } finally {
    restoring.value = false
  }
}
</script>

<template>
  <details class="disc sec py-1.5" aria-labelledby="h-restore">
    <summary><h2 id="h-restore" class="section-title">Restore points</h2></summary>
    <div class="space-y-2 pb-2 pt-1">
      <p v-if="kept" class="meta">Kaisen keeps the last <span class="mono">{{ kept }}</span>. For the full history, send this build's output to GitHub in Output settings.</p>
      <div v-if="error" class="alert-error flex items-center justify-between gap-2">
        <span>{{ error }}</span>
        <IconButton label="Retry" sm @click="load" />
      </div>
      <p v-else-if="items === null" class="text-sm text-zinc-300 motion-safe:animate-pulse">Loading…</p>
      <p v-else-if="items.length === 0" class="text-sm text-zinc-300">Restore points are taken when a run starts.</p>
      <ul v-else class="border-t border-pico-border">
        <li v-for="s in items" :key="s.id" class="border-b border-pico-border py-1.5">
          <div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
            <div class="min-w-0 text-sm">
              <span class="font-medium text-zinc-100">{{ label(s) }}</span>
              <!-- a restore point is never in the future: clamp clock skew -->
              <span class="mono block text-zinc-400">{{ relTime(s.created_at, Math.max(now, parseTs(s.created_at).getTime() || now)) }} · {{ s.files_count }} files · {{ fmtSize(s.bytes) }}</span>
            </div>
            <div class="row-acts">
              <button type="button" class="row-act" :aria-expanded="viewing === s.id" :aria-label="`${viewing === s.id ? 'Hide' : 'View'} changes since ${label(s).toLowerCase()}`" @click="viewing = viewing === s.id ? null : s.id">{{ viewing === s.id ? 'Hide changes' : 'View changes' }}</button>
              <button type="button" class="row-act" :disabled="!!blocked" :title="blocked || undefined" :aria-label="`Restore ${label(s).toLowerCase()}`" @click="target = s">Restore</button>
            </div>
          </div>
          <BuildDiffPanel v-if="viewing === s.id" :load="() => getSnapshotDiff(buildId, s.id)" title="Changes from this restore point to the current files" />
        </li>
      </ul>
      <p v-if="blocked && items && items.length" class="help">{{ blocked }}</p>
      <p v-if="restoreError" class="alert-error" role="alert">{{ restoreError }}</p>
      <div v-if="done" class="alert-success flex flex-wrap items-center justify-between gap-2" role="status">
        <span>{{ done }} The preview still runs the old version.</span>
        <button v-if="canDeploy" type="button" class="btn-secondary btn-sm" :disabled="deploying" @click="emit('deploy')">{{ deploying ? 'Deploying…' : 'Deploy now' }}</button>
      </div>
    </div>
    <ConfirmDialog
      :open="!!target"
      title="Restore these files?"
      :message="`Replace the current files with the files from ${target ? label(target).toLowerCase() : 'this restore point'}? The current files are saved as a new restore point first, so you can undo this. The preview keeps running the old version until you deploy.`"
      confirm-label="Restore"
      :busy="restoring"
      busy-label="Restoring…"
      @confirm="doRestore"
      @cancel="target = null"
    />
  </details>
</template>
