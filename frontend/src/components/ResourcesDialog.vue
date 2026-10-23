<script setup lang="ts">
import { computed, reactive, ref, useId, watch } from 'vue'
import { useModal } from '../composables/useModal'
import { dbEngineLabel } from '../composables/appUi'
import type { AdminApp, ResourcesPatch } from '../types'

const props = defineProps<{ app: AdminApp | null; error?: string }>()
const emit = defineEmits<{ save: [body: ResourcesPatch, replicas: number | null]; cancel: [] }>()

const uid = useId()
const dlg = ref<HTMLDialogElement | null>(null)
const m = useModal(dlg, () => !!props.app, () => emit('cancel'))
// Platform defaults for this app's engine come from the backend (resources.defaults).
const D = computed(() => props.app?.resources.defaults)
// Cpu/memory defaults double as the platform cap used for the "exceeds" warning.
const CAPS = computed(() => ({ cpu: D.value?.cpu_limit_m ?? 0, mem: D.value?.mem_limit_mi ?? 0 }))
type Key = 'cpu_request_m' | 'cpu_limit_m' | 'mem_request_mi' | 'mem_limit_mi'
const keys: Key[] = ['cpu_request_m', 'cpu_limit_m', 'mem_request_mi', 'mem_limit_mi']
const fields: { key: Key; label: string; unit: string; min: number; max: number }[] = [
  { key: 'cpu_request_m', label: 'CPU request', unit: 'm', min: 10, max: 1000 },
  { key: 'cpu_limit_m', label: 'CPU limit', unit: 'm', min: 10, max: 1000 },
  { key: 'mem_request_mi', label: 'Memory request', unit: 'Mi', min: 32, max: 2048 },
  { key: 'mem_limit_mi', label: 'Memory limit', unit: 'Mi', min: 32, max: 2048 },
]

// Extra limits (null = platform default). `unit` is MiB or MB; `max` is the backend ceiling.
type XKey = 'ephemeral_limit_mi' | 'db_max_connections' | 'db_quota_mb' | 'storage_quota_mb' | 'image_size_mb'
const xFields: { key: XKey; label: string; unit: string; min: number; max: number; show: 'all' | 'db' | 'storage'; restart?: boolean }[] = [
  { key: 'ephemeral_limit_mi', label: 'Scratch disk', unit: 'MiB', min: 64, max: 2048, show: 'all', restart: true },
  { key: 'image_size_mb', label: 'Max image size', unit: 'MB', min: 1, max: 2048, show: 'all' },
  { key: 'db_max_connections', label: 'DB connections', unit: '', min: 1, max: 25, show: 'db' },
  { key: 'db_quota_mb', label: 'DB quota', unit: 'MB', min: 1, max: 1024, show: 'db' },
  { key: 'storage_quota_mb', label: 'Bucket quota', unit: 'MB', min: 1, max: 51200, show: 'storage' },
]
// Show sizes in GB once they are large; connections have no unit.
const human = (n: number, unit: string) =>
  !unit ? String(n) : n >= 1024 ? `${+(n / 1024).toFixed(2)} GB` : `${n} ${unit}`
const visibleX = computed(() =>
  xFields.filter((f) => f.show === 'all' || (f.show === 'db' ? props.app?.want_db : props.app?.want_storage)),
)

const vals = reactive<Record<Key, string>>({ cpu_request_m: '', cpu_limit_m: '', mem_request_mi: '', mem_limit_mi: '' })
const xvals = reactive<Record<XKey, string>>({ ephemeral_limit_mi: '', db_max_connections: '', db_quota_mb: '', storage_quota_mb: '', image_size_mb: '' })
const replicasVal = ref('')
watch(
  () => props.app,
  (a) => {
    for (const k of keys) vals[k] = a && a.resources[k] !== a.resources.defaults[k] ? String(a.resources[k]) : ''
    for (const f of xFields) xvals[f.key] = a && a.resources[f.key] !== a.resources.defaults[f.key] ? String(a.resources[f.key]) : ''
    replicasVal.value = a ? String(a.replicas) : ''
  },
  { immediate: true },
)

const parse = (v: string) => (/^\d+$/.test(v.trim()) ? Number(v.trim()) : null)
const num = (k: Key) => parse(vals[k])
const kBad = (f: (typeof fields)[number]) => {
  const v = vals[f.key].trim()
  if (v === '') return false
  const n = parse(v)
  return n === null || n < f.min || n > f.max
}
const xnum = (k: XKey) => parse(xvals[k])
const xBad = (f: (typeof xFields)[number]) => {
  const v = xvals[f.key].trim()
  if (v === '') return false
  const n = parse(v)
  return n === null || n < f.min || n > f.max
}
const replicasBad = computed(() => {
  const v = replicasVal.value.trim()
  if (v === '') return false
  const n = parse(v)
  return n === null || n < 1 || n > 3
})
const invalid = computed(
  () => fields.some(kBad) || visibleX.value.some(xBad) || replicasBad.value,
)
const exceeds = computed(
  () =>
    (num('cpu_request_m') ?? 0) > CAPS.value.cpu || (num('cpu_limit_m') ?? 0) > CAPS.value.cpu ||
    (num('mem_request_mi') ?? 0) > CAPS.value.mem || (num('mem_limit_mi') ?? 0) > CAPS.value.mem,
)
// cpu/memory/scratch-disk changes roll the app's pods.
const restarts = computed(() => {
  const r = props.app?.resources
  if (!r) return false
  const cpuMem = keys.some((k) => (num(k) ?? r.defaults[k]) !== r[k])
  const eph = (xnum('ephemeral_limit_mi') ?? r.defaults.ephemeral_limit_mi) !== r.ephemeral_limit_mi
  return cpuMem || eph
})

function save() {
  if (invalid.value) return
  const body: ResourcesPatch = Object.fromEntries(keys.map((k) => [k, num(k)]))
  for (const f of visibleX.value) body[f.key] = xnum(f.key)
  const rep = parse(replicasVal.value)
  emit('save', body, rep !== null && rep !== props.app?.replicas ? rep : null)
}
function reset() {
  const body: ResourcesPatch = { cpu_request_m: null, cpu_limit_m: null, mem_request_mi: null, mem_limit_mi: null }
  for (const f of visibleX.value) body[f.key] = null
  emit('save', body, null)
}
</script>

<template>
  <dialog
    ref="dlg"
    :aria-labelledby="`${uid}-title`"
    class="m-auto w-[calc(100%-2rem)] max-w-lg rounded-lg border border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/70"
    @cancel="m.onNativeCancel"
    @click="m.onBackdrop"
  >
    <form v-if="app" class="p-4" @submit.prevent="save">
      <h2 :id="`${uid}-title`" class="section-title">Limits: {{ app.name }}</h2>
      <p class="help">Blank = platform default (shown as placeholder).</p>
      <div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div v-for="f in fields" :key="f.key">
          <label :for="`${uid}-${f.key}`" class="label">{{ f.label }} ({{ f.unit }})</label>
          <input
            :id="`${uid}-${f.key}`"
            v-model="vals[f.key]"
            type="text"
            inputmode="numeric"
            pattern="[0-9]*"
            :placeholder="`${app.resources.defaults[f.key]}${f.unit}`"
            class="input"
            :aria-invalid="kBad(f) ? 'true' : undefined"
          />
          <p class="help">{{ f.min }} to {{ human(f.max, f.unit) }}</p>
        </div>
      </div>
      <h3 class="label mt-4">Replicas</h3>
      <div class="mt-1 max-w-[12rem]">
        <input
          :id="`${uid}-replicas`" v-model="replicasVal" type="text" inputmode="numeric" pattern="[0-9]*"
          aria-label="Replicas" :placeholder="String(app.replicas)" class="input" :aria-invalid="replicasBad ? 'true' : undefined"
        />
        <p class="help">1 to 3. Scaling does not restart existing pods.</p>
      </div>
      <h3 class="label mt-4">Disk, image and data</h3>
      <div class="mt-1 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div v-for="f in visibleX" :key="f.key">
          <label :for="`${uid}-${f.key}`" class="label">{{ f.label }}{{ f.key === 'db_max_connections' ? ` (${dbEngineLabel(app.db_engine)})` : '' }}{{ f.unit ? ` (${f.unit})` : '' }}</label>
          <input
            :id="`${uid}-${f.key}`"
            v-model="xvals[f.key]"
            type="text"
            inputmode="numeric"
            pattern="[0-9]*"
            :placeholder="human(app.resources.defaults[f.key], f.unit)"
            class="input"
            :aria-invalid="xBad(f) ? 'true' : undefined"
          />
          <p class="help">{{ f.min }} to {{ human(f.max, f.unit) }}</p>
        </div>
      </div>
      <p v-if="invalid" class="field-error" role="alert">Enter whole numbers within the shown ranges.</p>
      <p v-if="restarts" class="alert-warn mt-3 text-xs" role="status">
        Changing CPU, memory or scratch disk restarts this app: its pods are replaced in a rolling restart.
      </p>
      <p v-if="exceeds" class="alert-warn mt-3 text-xs" role="status">
        Above the platform cap ({{ CAPS.cpu }}m CPU / {{ CAPS.mem }}Mi memory). The cluster may not have room to run this app.
      </p>
      <p v-if="error" class="alert-error mt-3 text-xs" role="alert">{{ error }}</p>
      <div class="mt-5 flex flex-wrap items-center justify-between gap-2">
        <button type="button" class="link -ml-1 rounded px-1 text-sm" @click="reset">Reset to defaults</button>
        <div class="flex gap-2">
          <button type="button" class="btn-secondary" @click="emit('cancel')">Cancel</button>
          <button type="submit" :disabled="invalid" class="btn-primary">{{ restarts ? 'Save and restart' : 'Save' }}</button>
        </div>
      </div>
    </form>
  </dialog>
</template>
