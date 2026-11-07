<script setup lang="ts">
import { computed, ref, useId } from 'vue'
import type { OwnerControl, OwnerControls, OwnerKey } from '../types'

// Owner-adjustable limits (CPU, memory, scratch disk, object storage quota). Default = the ceiling.
// v-model is a plain record of the current numbers; the parent decides what to send.
const props = defineProps<{
  controls: OwnerControls
  modelValue: Partial<Record<OwnerKey, number>>
  showStorage: boolean
  original?: Partial<Record<OwnerKey, number>> // edit only: values the app has now
  baseline?: OwnerControls | null // what a new app gets; a different max here means an admin set the ceiling
  open?: boolean
  disabled?: boolean
  errors?: Record<string, string>
}>()
const emit = defineEmits<{ 'update:modelValue': [v: Partial<Record<OwnerKey, number>>] }>()
const uid = useId()
const isOpen = ref(!!props.open)

interface RowDef {
  key: OwnerKey
  label: string
  unit: string
  step: number
  gloss?: (n: number) => string
  help: string
}
const ROWS: RowDef[] = [
  { key: 'cpu_limit_m', label: 'CPU', unit: 'm', step: 10, gloss: (n) => `${+(n / 1000).toFixed(2)} CPU`, help: 'More CPU makes busy apps faster. Static sites need very little.' },
  { key: 'mem_limit_mi', label: 'Memory', unit: 'MB', step: 8, help: 'Too little memory makes the app crash and restart over and over. If you see that, raise it.' },
  { key: 'ephemeral_limit_mi', label: 'Scratch disk', unit: 'MB', step: 8, help: 'Temporary files. Cleared on every restart.' },
  { key: 'storage_quota_mb', label: 'Object storage quota', unit: 'MB', step: 1, help: 'Size cap for the bucket. Lowering it never deletes files.' },
]
type Row = RowDef & { c: OwnerControl }
const rows = computed<Row[]>(() =>
  ROWS.flatMap((r) => {
    if (r.key === 'storage_quota_mb' && !props.showStorage) return []
    const c = props.controls[r.key]
    return c ? [{ ...r, c }] : []
  }),
)

const val = (r: Row) => props.modelValue[r.key] ?? r.c.max
function set(k: OwnerKey, n: number) {
  emit('update:modelValue', { ...props.modelValue, [k]: n })
}
// A range input only lands on min + k * step, so a ceiling off that grid (64 to 250 in steps of 8) would leave the thumb
// short of the end. End the track on the next notch up and let that last notch mean "the ceiling".
const trackMax = (r: Row) => r.c.min + Math.ceil((r.c.max - r.c.min) / r.step) * r.step
const trackValue = (r: Row) => (val(r) >= r.c.max ? trackMax(r) : val(r))
function onSlide(r: Row, e: Event) {
  set(r.key, Math.min(r.c.max, Number((e.target as HTMLInputElement).value)))
}
function onNumber(r: Row, e: Event) {
  const raw = (e.target as HTMLInputElement).value.trim()
  if (raw === '' || !Number.isFinite(Number(raw))) return
  set(r.key, Number(raw)) // clamped on change, so typing "3" on the way to "384" isn't snapped mid-way
}
function onNumberCommit(r: Row, e: Event) {
  const el = e.target as HTMLInputElement
  const raw = el.value.trim()
  const n = raw === '' || !Number.isFinite(Number(raw)) ? r.c.max : Math.min(r.c.max, Math.max(r.c.min, Math.round(Number(raw))))
  set(r.key, n)
  el.value = String(n)
}
const bad = (r: Row) => {
  const n = val(r)
  return !Number.isInteger(n) || n < r.c.min || n > r.c.max
}
function resetAll() {
  const out: Partial<Record<OwnerKey, number>> = {}
  for (const r of rows.value) out[r.key] = r.c.max
  emit('update:modelValue', out)
}

const fmt = (r: Row, n: number) => (r.unit === 'm' ? `${n}m` : `${n} MB`)
const summary = computed(() => rows.value.map((r) => `${r.key === 'storage_quota_mb' ? 'Storage' : r.label} ${fmt(r, val(r))}`).join(' · '))
const lowered = computed(() => rows.value.some((r) => val(r) < r.c.max))
const restarts = computed(
  () =>
    !!props.original &&
    rows.value.some((r) => r.key !== 'storage_quota_mb' && props.original?.[r.key] !== undefined && val(r) !== props.original[r.key]),
)
const adminCeiling = (r: Row) => {
  const b = props.baseline?.[r.key]
  return !!props.original && !!b && b.max !== r.c.max
}
</script>

<template>
  <details class="disc border-y border-pico-border py-2" :open="isOpen" @toggle="isOpen = ($event.target as HTMLDetailsElement).open">
    <summary class="cursor-pointer text-sm font-semibold text-white">
      Resources
      <span class="ml-1 text-xs font-normal text-zinc-400">{{ summary }}<template v-if="lowered"> (lowered)</template></span>
    </summary>
    <div class="mt-3 space-y-4">
      <p class="help mt-0!">Each limit starts at its maximum. Lower one if your app needs less, such as a static site.</p>
      <div v-for="r in rows" :key="r.key">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <label :for="`${uid}-${r.key}-n`" class="label mb-0!">{{ r.label }} ({{ r.unit }})</label>
          <div class="flex items-center gap-2">
            <span v-if="r.gloss" class="meta">{{ r.gloss(val(r)) }}</span>
            <input
              :id="`${uid}-${r.key}-n`"
              type="number"
              inputmode="numeric"
              :min="r.c.min"
              :max="r.c.max"
              :step="r.step"
              :value="val(r)"
              :disabled="disabled"
              class="input mono w-28!"
              :aria-invalid="bad(r) || !!errors?.[`resources.${r.key}`] || undefined"
              :aria-describedby="`${uid}-${r.key}-help`"
              @input="onNumber(r, $event)"
              @change="onNumberCommit(r, $event)"
            />
          </div>
        </div>
        <input
          type="range"
          class="range mt-1"
          :min="r.c.min"
          :max="trackMax(r)"
          :step="r.step"
          :value="trackValue(r)"
          :disabled="disabled"
          :aria-label="`${r.label} slider`"
          :aria-valuetext="fmt(r, val(r))"
          @input="onSlide(r, $event)"
        />
        <div class="flex justify-between text-xs text-zinc-400" aria-hidden="true">
          <span>{{ fmt(r, r.c.min) }}</span>
          <span>{{ fmt(r, r.c.max) }}</span>
        </div>
        <p v-if="errors?.[`resources.${r.key}`]" class="field-error" role="alert">{{ errors[`resources.${r.key}`] }}</p>
        <p v-else-if="bad(r)" class="field-error" role="alert">Use a whole number from {{ r.c.min }} to {{ r.c.max }}.</p>
        <p :id="`${uid}-${r.key}-help`" class="help">
          {{ r.help }}
          <template v-if="adminCeiling(r)"> Your limit on this app is {{ fmt(r, r.c.max) }}.</template>
        </p>
      </div>
      <p v-if="restarts" class="alert-warn text-xs" role="status">Changing CPU, memory or scratch disk restarts the app when you save.</p>
      <button type="button" class="link text-sm" :disabled="disabled" @click="resetAll">Reset all to maximum</button>
    </div>
  </details>
</template>
