<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { useModal } from '../composables/useModal'
import { getAppBackups, putAppBackups } from '../api/admin'
import { ApiError } from '../api/client'
import { useToastStore } from '../stores/toast'
import type { AdminApp, BackupsInfo } from '../types'

// Admin control of one app's backups: lock off (reason shown to the owner) and per-app storage ceiling override.
const props = defineProps<{ app: AdminApp | null }>()
const emit = defineEmits<{ close: [] }>()
const uid = useId()
const toast = useToastStore()
const dlg = ref<HTMLDialogElement | null>(null)
const m = useModal(dlg, () => !!props.app, () => !busy.value && emit('close'))
const info = ref<BackupsInfo | null>(null)
const error = ref('')
const busy = ref(false)
const reason = ref('')
const capVal = ref('')
const capUnit = ref<'MB' | 'GB'>('MB')
const MB = 1048576
const size = (n: number) => (n >= 1024 * MB ? `${(n / 1024 / MB).toFixed(2)} GB` : `${(n / MB).toFixed(1)} MB`)

function fill(i: BackupsInfo) {
  info.value = i
  reason.value = i.admin_disabled_reason ?? ''
  capVal.value = i.cap_override ? String(i.cap_bytes >= 1024 * MB && i.cap_bytes % (1024 * MB) === 0 ? i.cap_bytes / 1024 / MB : Math.round(i.cap_bytes / MB)) : ''
  capUnit.value = i.cap_override && i.cap_bytes >= 1024 * MB && i.cap_bytes % (1024 * MB) === 0 ? 'GB' : 'MB'
}
watch(() => props.app, async (a) => {
  info.value = null
  error.value = ''
  if (!a) return
  try {
    fill(await getAppBackups(a.id))
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load backup settings."
  }
}, { immediate: true })

const locked = computed(() => !!info.value?.admin_disabled)
const capBytes = computed(() => {
  const v = capVal.value.trim()
  if (!/^\d+(\.\d+)?$/.test(v)) return null
  return Math.round(Number(v) * MB * (capUnit.value === 'GB' ? 1024 : 1))
})
const capBad = computed(() => capVal.value.trim() !== '' && (capBytes.value === null || capBytes.value < MB))
const capChanged = computed(() => capVal.value.trim() !== '' && !capBad.value && (!info.value?.cap_override || capBytes.value !== info.value.cap_bytes))

async function put(body: Parameters<typeof putAppBackups>[1], ok: string) {
  if (!props.app || busy.value) return
  busy.value = true
  error.value = ''
  try {
    fill(await putAppBackups(props.app.id, body))
    toast.success(ok)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Try again.'
  } finally {
    busy.value = false
  }
}
const lock = () => put({ admin_disabled: true, reason: reason.value.trim() }, `Backups disabled for ${props.app?.name}.`)
const unlock = () => put({ admin_disabled: false }, `Backups re-enabled for ${props.app?.name}.`)
const saveCap = () => put({ max_bytes: capBytes.value }, 'Storage ceiling saved.')
const clearCap = () => put({ max_bytes: null }, 'Storage ceiling reset to the platform default.')
</script>

<template>
  <dialog
    ref="dlg"
    :aria-labelledby="`${uid}-title`"
    class="m-auto w-[calc(100%-2rem)] max-w-md rounded-lg border border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/70"
    @cancel="m.onNativeCancel"
    @click="m.onBackdrop"
  >
    <div v-if="app" class="p-4">
      <h2 :id="`${uid}-title`" class="section-title">Backups: {{ app.name }}</h2>
      <p v-if="error" role="alert" class="alert-error mt-2">{{ error }}</p>
      <p v-if="!info && !error" class="mt-2 text-sm text-zinc-400" role="status">Loading…</p>
      <template v-if="info">
        <p class="help">
          Using {{ size(info.repo_bytes ?? 0) }} of {{ size(info.cap_bytes) }}<template v-if="info.cap_override"> (override)</template>
          · {{ info.held ?? 0 }} held<template v-if="info.storage_full"> · <span class="text-amber-300">full</span></template>
        </p>

        <form class="mt-4" @submit.prevent="locked ? unlock() : lock()">
          <h3 class="label mb-1!">Backups are {{ locked ? 'disabled by an administrator' : 'allowed' }}</h3>
          <p v-if="locked" class="help mt-0!">The owner sees: {{ info.admin_disabled_message }}</p>
          <p v-else class="help mt-0!">Disabling stops scheduled and manual backups and keeps existing ones. The owner can't turn them back on.</p>
          <template v-if="!locked">
            <label :for="`${uid}-reason`" class="label mt-2">Reason (shown to the owner)</label>
            <input :id="`${uid}-reason`" v-model="reason" type="text" maxlength="255" class="input" autocomplete="off" />
          </template>
          <button type="submit" :class="[locked ? 'btn-secondary' : 'btn-danger-solid', 'mt-2']" :disabled="busy || (!locked && !reason.trim())">
            {{ locked ? 'Re-enable backups' : 'Disable backups' }}
          </button>
        </form>

        <form class="mt-5 border-t border-pico-border pt-4" @submit.prevent="saveCap">
          <h3 class="label mb-1!">Storage ceiling</h3>
          <p class="help mt-0!">Blank uses the platform default. Minimum 1 MB; can't exceed the platform total.</p>
          <div class="mt-2 flex items-center gap-2">
            <input :id="`${uid}-cap`" v-model="capVal" type="text" inputmode="decimal" class="input w-28!" aria-label="Storage ceiling" :aria-invalid="capBad" placeholder="default" autocomplete="off" />
            <select v-model="capUnit" class="input w-20!" aria-label="Unit"><option>MB</option><option>GB</option></select>
            <button type="submit" class="btn-secondary" :disabled="busy || !capChanged">Save</button>
            <button type="button" class="btn-secondary" :disabled="busy || !info.cap_override" @click="clearCap">Clear override</button>
          </div>
          <p v-if="capBad" class="mt-1 text-xs text-danger-light">Enter a number of at least 1 MB.</p>
        </form>
      </template>
      <div class="mt-5 flex justify-end"><button type="button" class="btn-secondary" :disabled="busy" @click="emit('close')">Close</button></div>
    </div>
  </dialog>
</template>
