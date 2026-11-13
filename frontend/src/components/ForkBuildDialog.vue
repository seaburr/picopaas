<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import ConfirmDialog from './ConfirmDialog.vue'
import { ApiError } from '../api/client'
import { listSnapshots } from '../api/kaisen'
import { parseTs, relTime } from '../composables/appUi'
import { stampTitle } from '../utils/format'
import type { KaisenSnapshot } from '../types'

// "Fork this build": name + source (current files or one restore point). Loads the restore points itself when opened.
const NAME_MAX = 60 // same limit as the New build form
const props = defineProps<{
  open: boolean
  buildId: number
  buildName: string
  active: boolean // a run is active: the server refuses a fork of the current files
  now: number
  preselect?: number | null // restore point id to start on
  busy?: boolean
  error?: string
}>()
const emit = defineEmits<{ confirm: [p: { name?: string; snapshot_id?: number }]; cancel: [] }>()

const uid = useId()
const name = ref('')
const source = ref<'current' | number>('current')
const items = ref<KaisenSnapshot[] | null>(null)
const loadError = ref('')
const label = (s: KaisenSnapshot) => (s.kind === 'run_start' ? `Before run #${s.run_number ?? s.run_id ?? '?'}` : 'Before restore')
const ACTIVE_WHY = 'A run is in progress. Fork from a restore point, or wait for it to finish.'

async function load() {
  loadError.value = ''
  items.value = null
  try {
    items.value = await listSnapshots(props.buildId)
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : "Couldn't load restore points."
  }
}
watch(() => props.open, (o) => {
  if (!o) return
  name.value = `${props.buildName} fork`.slice(0, NAME_MAX)
  source.value = props.preselect ?? 'current'
  void load().then(() => {
    // Current files are off during a run: land on the newest restore point instead.
    if (props.active && source.value === 'current' && items.value?.length) source.value = items.value[0].id
  })
}, { immediate: true })

const valid = computed(() => !!name.value.trim() && (source.value !== 'current' || !props.active))
function confirm() {
  const n = name.value.trim()
  emit('confirm', { name: n || undefined, ...(source.value === 'current' ? {} : { snapshot_id: source.value }) })
}
</script>

<template>
  <ConfirmDialog
    :open="open"
    title="Fork this build"
    message="Start a new build from this one's brief and files."
    confirm-label="Fork"
    busy-label="Forking…"
    :busy="busy"
    :confirm-disabled="!valid"
    @confirm="confirm"
    @cancel="emit('cancel')"
  >
    <div class="mt-3">
      <label :for="`${uid}-name`" class="label">Name</label>
      <input :id="`${uid}-name`" v-model="name" class="input" :maxlength="NAME_MAX" autocomplete="off" data-autofocus />
    </div>
    <fieldset class="mt-3">
      <legend class="label">Files from</legend>
      <label class="flex items-start gap-2 py-0.5 text-sm" :class="active ? 'text-zinc-400' : 'text-zinc-200'">
        <input v-model="source" type="radio" :name="`${uid}-src`" value="current" class="check mt-0.5" :disabled="active" aria-describedby="fk-active" /> Current files
      </label>
      <p v-if="active" id="fk-active" class="help !mt-0 pl-6" data-testid="fork-active">{{ ACTIVE_WHY }}</p>
      <p v-if="items === null && !loadError" class="help pl-6 motion-safe:animate-pulse">Loading restore points…</p>
      <p v-if="loadError" class="field-error pl-6" role="alert">{{ loadError }} <button type="button" class="link" @click="load">Retry</button></p>
      <label v-for="s in items ?? []" :key="s.id" class="flex items-start gap-2 py-0.5 text-sm text-zinc-200" :data-snapshot="s.id">
        <input v-model="source" type="radio" :name="`${uid}-src`" :value="s.id" class="check mt-0.5" />
        <span>Restore point: {{ label(s) }} <span class="mono text-zinc-400" :title="stampTitle(s.created_at)">{{ relTime(s.created_at, Math.max(now, parseTs(s.created_at).getTime() || now)) }}</span></span>
      </label>
      <p v-if="items && items.length === 0" class="help pl-6">No restore points yet.</p>
    </fieldset>
    <ul class="mt-3 space-y-0.5 text-xs text-zinc-300" aria-label="What a fork copies">
      <li>Copied: the brief and the files.</li>
      <li>Not copied: runs and history, spend, restore points, build secret values, uploads, GitHub output settings.</li>
      <li>The fork deploys as a new app with a new address and an empty database, and uses an app slot.</li>
      <li>It costs nothing until you run it.</li>
    </ul>
    <p v-if="error" class="alert-error mt-3" role="alert">{{ error }}</p>
  </ConfirmDialog>
</template>
