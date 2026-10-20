<script setup lang="ts">
import { computed, reactive, ref, useId, watch } from 'vue'
import { useModal } from '../composables/useModal'
import { useAuthStore } from '../stores/auth'
import type { AdminApp, ResourcesPatch } from '../types'

const props = defineProps<{ app: AdminApp | null; error?: string }>()
const emit = defineEmits<{ save: [body: ResourcesPatch]; cancel: [] }>()

const auth = useAuthStore()
const uid = useId()
const dlg = ref<HTMLDialogElement | null>(null)
const m = useModal(dlg, () => !!props.app, () => emit('cancel'))
// Caps come from /auth/me limits when the backend provides them; fallback matches app/config.py.
const CAPS = computed(() => ({
  cpu: auth.user?.limits?.cpu_limit_m ?? 100,
  mem: auth.user?.limits?.mem_limit_mi ?? 384,
}))
const DEFAULTS = { cpu_request_m: 64, cpu_limit_m: 100, mem_request_mi: 128, mem_limit_mi: 384 }
type Key = keyof typeof DEFAULTS
const keys: Key[] = ['cpu_request_m', 'cpu_limit_m', 'mem_request_mi', 'mem_limit_mi']
const fields: { key: Key; label: string; unit: string }[] = [
  { key: 'cpu_request_m', label: 'CPU request', unit: 'm' },
  { key: 'cpu_limit_m', label: 'CPU limit', unit: 'm' },
  { key: 'mem_request_mi', label: 'Memory request', unit: 'Mi' },
  { key: 'mem_limit_mi', label: 'Memory limit', unit: 'Mi' },
]

const vals = reactive<Record<Key, string>>({ cpu_request_m: '', cpu_limit_m: '', mem_request_mi: '', mem_limit_mi: '' })
watch(
  () => props.app,
  (a) => {
    for (const k of keys) vals[k] = a?.resources?.overridden ? String(a.resources[k]) : ''
  },
  { immediate: true },
)

const num = (k: Key) => (/^\d+$/.test(vals[k].trim()) ? Number(vals[k].trim()) : null)
const invalid = computed(() => keys.some((k) => vals[k].trim() !== '' && num(k) === null))
const exceeds = computed(
  () =>
    (num('cpu_request_m') ?? 0) > CAPS.value.cpu || (num('cpu_limit_m') ?? 0) > CAPS.value.cpu ||
    (num('mem_request_mi') ?? 0) > CAPS.value.mem || (num('mem_limit_mi') ?? 0) > CAPS.value.mem,
)

function save() {
  if (invalid.value) return
  emit('save', Object.fromEntries(keys.map((k) => [k, num(k)])) as ResourcesPatch)
}
function reset() {
  emit('save', { cpu_request_m: null, cpu_limit_m: null, mem_request_mi: null, mem_limit_mi: null })
}
</script>

<template>
  <dialog
    ref="dlg"
    :aria-labelledby="`${uid}-title`"
    class="m-auto w-[calc(100%-2rem)] max-w-md rounded-lg border border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/70"
    @cancel="m.onNativeCancel"
    @click="m.onBackdrop"
  >
    <form v-if="app" class="p-4" @submit.prevent="save">
      <h2 :id="`${uid}-title`" class="section-title">Resources: {{ app.name }}</h2>
      <p class="help">Leave a field empty to use the default (shown as placeholder).</p>
      <div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div v-for="f in fields" :key="f.key">
          <label :for="`${uid}-${f.key}`" class="label">{{ f.label }} ({{ f.unit }})</label>
          <input
            :id="`${uid}-${f.key}`"
            v-model="vals[f.key]"
            type="text"
            inputmode="numeric"
            pattern="[0-9]*"
            :placeholder="`${DEFAULTS[f.key]}${f.unit}`"
            class="input"
            :aria-invalid="vals[f.key].trim() !== '' && num(f.key) === null ? 'true' : undefined"
          />
        </div>
      </div>
      <p v-if="invalid" class="field-error" role="alert">Enter whole numbers only.</p>
      <p v-if="exceeds" class="alert-warn mt-3 text-xs" role="status">
        Above the platform cap ({{ CAPS.cpu }}m CPU / {{ CAPS.mem }}Mi memory). The scheduler may not fit this app.
      </p>
      <p v-if="error" class="alert-error mt-3 text-xs" role="alert">{{ error }}</p>
      <div class="mt-5 flex flex-wrap items-center justify-between gap-2">
        <button type="button" class="link -ml-1 rounded px-1 text-sm" @click="reset">Reset to defaults</button>
        <div class="flex gap-2">
          <button type="button" class="btn-secondary" @click="emit('cancel')">Cancel</button>
          <button type="submit" :disabled="invalid" class="btn-primary">Save</button>
        </div>
      </div>
    </form>
  </dialog>
</template>
