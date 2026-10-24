<script setup lang="ts">
import { computed, reactive, ref, useId, watch } from 'vue'
import { useModal } from '../composables/useModal'
import { dbEngineLabel } from '../composables/appUi'
import { useAuthStore } from '../stores/auth'
import { getSettings } from '../api/admin'
import type { AdminApp, ResourcesPatch } from '../types'

const props = defineProps<{ app: AdminApp | null; error?: string }>()
const emit = defineEmits<{ save: [body: ResourcesPatch, replicas: number | null]; cancel: [] }>()

const uid = useId()
const dlg = ref<HTMLDialogElement | null>(null)
const m = useModal(dlg, () => !!props.app, () => emit('cancel'))
// Platform defaults for this app's engine come from the backend (resources.defaults).
const D = computed(() => props.app?.resources.defaults)
// Cpu/memory defaults double as the platform cap used for the "exceeds" warning.
const auth = useAuthStore()
// Ceilings are admin settings (Admin > Settings); the numbers below are fallbacks until they load.
const ceil = reactive<Record<string, number>>({})
watch(() => props.app, async (a) => {
  if (!a) return
  try {
    for (const g of (await getSettings()).groups) for (const x of g.settings) if (x.type === 'int') ceil[x.key] = Number(x.value)
  } catch { /* keep fallbacks */ }
})
const mx = (f: { max: number; ck: string }) => ceil[f.ck] ?? f.max
const volMaxAdmin = computed(() => ceil.override_volume_max_gi ?? auth.user?.limits?.volume_override_max_gi ?? 10)
const replicasMax = computed(() => ceil.max_replicas ?? 3)
const CAPS = computed(() => ({ cpu: D.value?.cpu_limit_m ?? 0, mem: D.value?.mem_limit_mi ?? 0 }))
type Key = 'cpu_request_m' | 'cpu_limit_m' | 'mem_request_mi' | 'mem_limit_mi'
const keys: Key[] = ['cpu_request_m', 'cpu_limit_m', 'mem_request_mi', 'mem_limit_mi']
const fields: { key: Key; label: string; unit: string; min: number; max: number; ck: string }[] = [
  { key: 'cpu_request_m', label: 'CPU request', unit: 'm', min: 10, max: 1000, ck: 'cpu_limit_max_m' },
  { key: 'cpu_limit_m', label: 'CPU limit', unit: 'm', min: 10, max: 1000, ck: 'cpu_limit_max_m' },
  { key: 'mem_request_mi', label: 'Memory request', unit: 'Mi', min: 32, max: 2048, ck: 'mem_limit_max_mi' },
  { key: 'mem_limit_mi', label: 'Memory limit', unit: 'Mi', min: 32, max: 2048, ck: 'mem_limit_max_mi' },
]

// Extra limits (null = platform default). `unit` is MiB or MB; `max` is the backend ceiling.
type XKey = 'ephemeral_limit_mi' | 'db_max_connections' | 'db_quota_mb' | 'storage_quota_mb' | 'image_size_mb' | 'egress_mbit'
const xFields: { key: XKey; label: string; unit: string; min: number; max: number; ck: string; show: 'all' | 'db' | 'storage'; restart?: boolean }[] = [
  { key: 'ephemeral_limit_mi', label: 'Scratch disk', unit: 'MiB', min: 64, max: 2048, ck: 'override_ephemeral_max_mi', show: 'all', restart: true },
  { key: 'egress_mbit', label: 'Outbound bandwidth', unit: 'Mbit/s', min: 1, max: 160, ck: 'override_egress_max_mbit', show: 'all', restart: true },
  { key: 'image_size_mb', label: 'Max image size', unit: 'MB', min: 1, max: 2048, ck: 'override_image_size_max_mb', show: 'all' },
  { key: 'db_max_connections', label: 'DB connections', unit: '', min: 1, max: 25, ck: 'override_db_connections_max', show: 'db' },
  { key: 'db_quota_mb', label: 'DB quota', unit: 'MB', min: 1, max: 1024, ck: 'override_db_quota_max_mb', show: 'db' },
  { key: 'storage_quota_mb', label: 'Blob storage quota', unit: 'MB', min: 1, max: 51200, ck: 'override_storage_quota_max_mb', show: 'storage' },
]
// Show sizes in GB once they are large; connections have no unit.
const human = (n: number, unit: string) =>
  !unit ? String(n) : n >= 1024 ? `${+(n / 1024).toFixed(2)} GB` : `${n} ${unit}`
const visibleX = computed(() =>
  xFields.filter((f) => f.show === 'all' || (f.show === 'db' ? props.app?.want_db : props.app?.want_storage)),
)

const vals = reactive<Record<Key, string>>({ cpu_request_m: '', cpu_limit_m: '', mem_request_mi: '', mem_limit_mi: '' })
const xvals = reactive<Record<XKey, string>>({ ephemeral_limit_mi: '', db_max_connections: '', db_quota_mb: '', storage_quota_mb: '', image_size_mb: '', egress_mbit: '' })
const replicasVal = ref('')
const volumeVal = ref('')
watch(
  () => props.app,
  (a) => {
    for (const k of keys) vals[k] = a && a.resources[k] !== a.resources.defaults[k] ? String(a.resources[k]) : ''
    for (const f of xFields) xvals[f.key] = a && a.resources[f.key] !== a.resources.defaults[f.key] ? String(a.resources[f.key]) : ''
    replicasVal.value = a ? String(a.replicas) : ''
    volumeVal.value = a?.volume ? String(a.volume.size_gi) : ''
  },
  { immediate: true },
)

const parse = (v: string) => (/^\d+$/.test(v.trim()) ? Number(v.trim()) : null)
const num = (k: Key) => parse(vals[k])
const kBad = (f: (typeof fields)[number]) => {
  const v = vals[f.key].trim()
  if (v === '') return false
  const n = parse(v)
  return n === null || n < f.min || n > mx(f)
}
const xnum = (k: XKey) => parse(xvals[k])
const xBad = (f: (typeof xFields)[number]) => {
  const v = xvals[f.key].trim()
  if (v === '') return false
  const n = parse(v)
  return n === null || n < f.min || n > mx(f)
}
const replicasBad = computed(() => {
  const v = replicasVal.value.trim()
  if (v === '') return false
  const n = parse(v)
  return n === null || n < 1 || n > replicasMax.value
})
const volumeBad = computed(() => {
  const v = volumeVal.value.trim()
  if (!props.app?.volume || v === '') return false
  const n = parse(v)
  return n === null || n < Math.max(1, props.app.volume.size_gi) || n > volMaxAdmin.value
})
const invalid = computed(
  () => fields.some(kBad) || visibleX.value.some(xBad) || replicasBad.value || volumeBad.value,
)
const exceeds = computed(
  () =>
    (num('cpu_request_m') ?? 0) > CAPS.value.cpu || (num('cpu_limit_m') ?? 0) > CAPS.value.cpu ||
    (num('mem_request_mi') ?? 0) > CAPS.value.mem || (num('mem_limit_mi') ?? 0) > CAPS.value.mem,
)
// cpu/memory/scratch-disk/bandwidth changes roll the app's pods.
const restarts = computed(() => {
  const r = props.app?.resources
  if (!r) return false
  const cpuMem = keys.some((k) => (num(k) ?? r.defaults[k]) !== r[k])
  const eph = (xnum('ephemeral_limit_mi') ?? r.defaults.ephemeral_limit_mi) !== r.ephemeral_limit_mi
  const bw = (xnum('egress_mbit') ?? r.defaults.egress_mbit) !== r.egress_mbit
  return cpuMem || eph || bw
})

function save() {
  if (invalid.value) return
  const body: ResourcesPatch = Object.fromEntries(keys.map((k) => [k, num(k)]))
  for (const f of visibleX.value) body[f.key] = xnum(f.key)
  const vol = parse(volumeVal.value)
  if (props.app?.volume && vol !== null && vol !== props.app.volume.size_gi) body.volume_size_gi = vol
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
          <p class="help">{{ f.min }} to {{ human(mx(f), f.unit) }}</p>
        </div>
      </div>
      <h3 class="label mt-5 border-t border-pico-border pt-4">Replicas</h3>
      <div class="max-w-[12rem]">
        <input
          :id="`${uid}-replicas`" v-model="replicasVal" type="text" inputmode="numeric" pattern="[0-9]*"
          aria-label="Replicas" :placeholder="String(app.replicas)" class="input" :aria-invalid="replicasBad ? 'true' : undefined"
        />
        <p class="help">1 to {{ replicasMax }}. Scaling does not restart existing pods.</p>
      </div>
      <h3 class="label mt-5 border-t border-pico-border pt-4">Disk, image and data</h3>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
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
          <p class="help">{{ f.min }} to {{ human(mx(f), f.unit) }}</p>
        </div>
      </div>
      <div v-if="app.volume" class="mt-3 max-w-[12rem]">
        <label :for="`${uid}-volume`" class="label">Persistent volume size (GiB)</label>
        <input
          :id="`${uid}-volume`" v-model="volumeVal" type="text" inputmode="numeric" pattern="[0-9]*"
          :placeholder="String(app.volume.size_gi)" class="input" :aria-invalid="volumeBad ? 'true' : undefined"
        />
        <p class="help">Grow-only: {{ app.volume.size_gi }} to {{ volMaxAdmin }}. Can't be shrunk.</p>
      </div>
      <p class="help mt-3">Outbound bandwidth applies to everything the app sends, including responses to visitors.</p>
      <p v-if="invalid" class="field-error" role="alert">Enter whole numbers within the shown ranges.</p>
      <p v-if="restarts" class="alert-warn mt-3 text-xs" role="status">
        Changing CPU, memory, scratch disk or bandwidth restarts this app: its pods are replaced in a rolling restart.
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
