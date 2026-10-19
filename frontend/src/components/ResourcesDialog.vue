<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import type { AdminApp, ResourcesPatch } from '../types'

const props = defineProps<{ app: AdminApp | null; error?: string }>()
const emit = defineEmits<{ save: [body: ResourcesPatch]; cancel: [] }>()

const CAPS = { cpu: 250, mem: 512 }
const DEFAULTS = { cpu_request_m: 64, cpu_limit_m: 250, mem_request_mi: 128, mem_limit_mi: 512 }
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
    (num('cpu_request_m') ?? 0) > CAPS.cpu || (num('cpu_limit_m') ?? 0) > CAPS.cpu ||
    (num('mem_request_mi') ?? 0) > CAPS.mem || (num('mem_limit_mi') ?? 0) > CAPS.mem,
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
  <div v-if="app" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4">
    <form class="w-full max-w-md rounded-lg border border-pico-border bg-pico-panel p-5 shadow-xl" @submit.prevent="save">
      <h2 class="text-base font-semibold text-white">Resources: {{ app.name }}</h2>
      <p class="mt-1 text-xs text-zinc-500">Leave a field empty to use the default (shown as placeholder).</p>
      <div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label v-for="f in fields" :key="f.key" class="flex flex-col gap-1 text-xs text-zinc-400">
          <span class="font-medium text-zinc-200">{{ f.label }} ({{ f.unit }})</span>
          <input
            v-model="vals[f.key]"
            type="text"
            inputmode="numeric"
            pattern="[0-9]*"
            :placeholder="`${DEFAULTS[f.key]}${f.unit}`"
            class="rounded-md border border-pico-border bg-pico-card px-2 py-1.5 text-sm text-white focus:border-accent focus:outline-none"
          />
        </label>
      </div>
      <p v-if="invalid" class="mt-3 text-xs text-danger">Enter whole numbers only.</p>
      <p v-if="exceeds" class="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-400">
        Exceeds platform caps in bootstrap.md (250m CPU / 512Mi memory).
      </p>
      <p v-if="error" class="mt-3 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">{{ error }}</p>
      <div class="mt-5 flex flex-wrap items-center justify-between gap-2">
        <button type="button" class="rounded-md border border-pico-border px-3 py-1.5 text-sm text-zinc-300 hover:bg-pico-card" @click="reset">
          Reset to defaults
        </button>
        <div class="flex gap-2">
          <button type="button" class="rounded-md border border-pico-border px-3 py-1.5 text-sm text-zinc-300 hover:bg-pico-card" @click="emit('cancel')">
            Cancel
          </button>
          <button type="submit" :disabled="invalid" class="rounded-md bg-accent-dark px-3 py-1.5 text-sm font-medium text-white hover:bg-accent disabled:opacity-50">
            Save
          </button>
        </div>
      </div>
    </form>
  </div>
</template>
