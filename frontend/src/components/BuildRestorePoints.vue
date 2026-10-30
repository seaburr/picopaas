<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Eye, RotateCw } from 'lucide-vue-next'
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
  <section class="card card-pad" aria-labelledby="h-restore">
    <h2 id="h-restore" class="section-title mb-2">Restore points</h2>
    <p v-if="kept" class="meta mb-2">Kaisen keeps the last {{ kept }}. For the full history, send this build's output to GitHub in Output settings.</p>
    <div v-if="error" class="alert-error flex items-center justify-between gap-2">
      <span>{{ error }}</span>
      <IconButton :icon="RotateCw" label="Retry" sm @click="load" />
    </div>
    <p v-else-if="items === null" class="text-sm text-zinc-300 motion-safe:animate-pulse">Loading…</p>
    <p v-else-if="items.length === 0" class="text-sm text-zinc-300">Restore points are taken when a run starts.</p>
    <ul v-else class="space-y-2">
      <li v-for="s in items" :key="s.id" class="rounded-md border border-pico-border px-3 py-2">
        <div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div class="min-w-0 text-sm">
            <span class="font-medium text-zinc-100">{{ label(s) }}</span>
            <!-- a restore point is never in the future: clamp clock skew -->
            <span class="meta mono block sm:ml-2 sm:inline">{{ relTime(s.created_at, Math.max(now, parseTs(s.created_at).getTime() || now)) }} · {{ s.files_count }} files · {{ fmtSize(s.bytes) }}</span>
          </div>
          <div class="flex items-center gap-2">
            <button type="button" class="btn-secondary btn-sm" :aria-expanded="viewing === s.id" :aria-label="`${viewing === s.id ? 'Hide' : 'View'} changes since ${label(s).toLowerCase()}`" @click="viewing = viewing === s.id ? null : s.id">
              <Eye :size="14" aria-hidden="true" />{{ viewing === s.id ? 'Hide changes' : 'View changes' }}
            </button>
            <button type="button" class="btn-secondary btn-sm" :disabled="!!blocked" :title="blocked || undefined" :aria-label="`Restore ${label(s).toLowerCase()}`" @click="target = s">Restore</button>
          </div>
        </div>
        <BuildDiffPanel v-if="viewing === s.id" :load="() => getSnapshotDiff(buildId, s.id)" title="Changes from this restore point to the current files" />
      </li>
    </ul>
    <p v-if="blocked && items && items.length" class="help mt-2">{{ blocked }}</p>
    <p v-if="restoreError" class="alert-error mt-2" role="alert">{{ restoreError }}</p>
    <div v-if="done" class="alert-success mt-2 flex flex-wrap items-center justify-between gap-2" role="status">
      <span>{{ done }} The preview still runs the old version.</span>
      <button v-if="canDeploy" type="button" class="btn-secondary btn-sm" :disabled="deploying" @click="emit('deploy')">{{ deploying ? 'Deploying…' : 'Deploy now' }}</button>
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
  </section>
</template>
