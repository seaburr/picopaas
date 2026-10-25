<script setup lang="ts">
import { ref, useId, watch } from 'vue'
import { useModal } from '../composables/useModal'
import { listUserApiKeys, revokeUserApiKey } from '../api/admin'
import { ApiError } from '../api/client'
import { useToastStore } from '../stores/toast'
import type { AdminApiKey, AdminUser } from '../types'
import { Ban } from 'lucide-vue-next'
import ConfirmDialog from './ConfirmDialog.vue'
import IconButton from './IconButton.vue'
import { stamp, stampTitle } from '../utils/format'

// Admin view of one user's API keys (read + revoke). `changed` fires after a revoke so the caller can refresh counts.
const props = defineProps<{ user: AdminUser | null }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const uid = useId()
const toast = useToastStore()
const dlg = ref<HTMLDialogElement | null>(null)
const m = useModal(dlg, () => !!props.user, () => !pending.value && emit('close'))
const keys = ref<AdminApiKey[]>([])
const loading = ref(false)
const error = ref('')
const pending = ref<AdminApiKey | null>(null)
const busy = ref(false)

async function load() {
  if (!props.user) return
  try {
    keys.value = (await listUserApiKeys(props.user.id)) ?? []
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load API keys."
  } finally {
    loading.value = false
  }
}
watch(() => props.user, (u) => {
  keys.value = []
  error.value = ''
  pending.value = null
  loading.value = !!u
  if (u) load()
}, { immediate: true })

async function confirmRevoke() {
  const k = pending.value
  if (!k || !props.user || busy.value) return
  busy.value = true
  try {
    await revokeUserApiKey(props.user.id, k.id)
    toast.success(`Revoked ${k.name || k.prefix + '…'}.`)
    pending.value = null
    await load()
    emit('changed')
  } catch (e) {
    pending.value = null
    toast.error(`Couldn't revoke the key: ${e instanceof ApiError ? e.message : 'try again'}`)
  } finally {
    busy.value = false
  }
}

const grid = 'md:grid md:grid-cols-[minmax(0,1fr)_8.5rem_4rem_8.5rem_8.5rem_9.5rem_2rem] md:items-center md:gap-3'
</script>

<template>
  <dialog
    ref="dlg"
    :aria-labelledby="`${uid}-title`"
    class="m-auto w-[calc(100%-2rem)] max-w-4xl rounded-lg border border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/70"
    @cancel="m.onNativeCancel"
    @click="m.onBackdrop"
  >
    <div v-if="user" class="p-4">
      <h2 :id="`${uid}-title`" class="section-title">API keys: {{ user.email }}</h2>
      <p v-if="error" role="alert" class="alert-error mt-2">{{ error }}</p>
      <p v-if="loading" class="mt-2 text-sm text-zinc-400" role="status">Loading…</p>
      <p v-else-if="!error && keys.length === 0" class="mt-2 text-sm text-zinc-400">This user has no API keys.</p>
      <div v-else-if="keys.length" class="card mt-3 overflow-hidden">
        <div :class="[grid, 'table-th row-pad hidden border-b border-pico-border']">
          <span>Name</span><span>Key</span><span>Grant</span><span>Created</span><span>Last used</span><span>Status</span><span class="sr-only">Actions</span>
        </div>
        <ul class="divide-y divide-pico-border">
          <li v-for="k in keys" :key="k.id" :class="[grid, 'row-pad flex flex-col text-sm']">
            <span class="truncate" :class="k.revoked_at ? 'text-zinc-400' : 'font-medium text-white'" :title="k.name || undefined">{{ k.name || '(unnamed)' }}</span>
            <code class="mono truncate text-zinc-200">{{ k.prefix }}…</code>
            <span><span :class="k.scope === 'read' ? 'badge-neutral' : 'badge-info'">{{ k.scope }}</span></span>
            <span class="mono text-zinc-300" :title="stampTitle(k.created_at)"><span class="meta md:hidden">Created </span>{{ stamp(k.created_at) }}</span>
            <span class="mono text-zinc-300" :title="k.last_used_at ? stampTitle(k.last_used_at) : undefined"><span class="meta md:hidden">Last used </span>{{ k.last_used_at ? stamp(k.last_used_at) : 'never' }}</span>
            <span v-if="k.revoked_at" class="mono text-zinc-400" :title="stampTitle(k.revoked_at)">Revoked {{ stamp(k.revoked_at) }}</span>
            <span v-else class="text-emerald-300">Active</span>
            <span class="flex justify-end"><IconButton v-if="!k.revoked_at" :icon="Ban" :label="`Revoke ${k.name || k.prefix + '…'}`" sm danger @click="pending = k" /></span>
          </li>
        </ul>
      </div>
      <div class="mt-4 flex justify-end"><button type="button" class="btn-secondary" @click="emit('close')">Close</button></div>
    </div>
  </dialog>
  <ConfirmDialog
    :open="pending !== null"
    title="Revoke API key"
    :message="`Revoke ${pending?.name || pending?.prefix + '…'} for ${user?.email}? Anything using it stops working immediately. This can't be undone.`"
    confirm-label="Revoke key"
    :busy="busy"
    danger
    @confirm="confirmRevoke"
    @cancel="pending = null"
  />
</template>
