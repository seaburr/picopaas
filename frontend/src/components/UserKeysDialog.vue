<script setup lang="ts">
import { ref, useId, watch } from 'vue'
import { useModal } from '../composables/useModal'
import { listUserApiKeys, revokeUserApiKey } from '../api/admin'
import { ApiError } from '../api/client'
import { useToastStore } from '../stores/toast'
import type { AdminApiKey, AdminUser } from '../types'
import ConfirmDialog from './ConfirmDialog.vue'
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
</script>

<template>
  <dialog
    ref="dlg"
    :aria-labelledby="`${uid}-title`"
    class="m-auto w-[calc(100%-2rem)] max-w-4xl rounded-md border border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/70"
    @cancel="m.onNativeCancel"
    @click="m.onBackdrop"
  >
    <div v-if="user" class="p-4">
      <h2 :id="`${uid}-title`" class="section-title">API keys: {{ user.email }}</h2>
      <p v-if="error" role="alert" class="alert-error mt-2">{{ error }}</p>
      <p v-if="loading" class="mt-2 text-sm text-zinc-400" role="status">Loading…</p>
      <p v-else-if="!error && keys.length === 0" class="mt-2 text-sm text-zinc-400">This user has no API keys.</p>
      <div v-else-if="keys.length" class="mt-3 overflow-x-auto">
        <table class="tbl min-w-[44rem]">
          <colgroup><col /><col class="w-36" /><col class="w-16" /><col class="w-36" /><col class="w-36" /><col class="w-40" /><col class="w-20" /></colgroup>
          <thead>
            <tr><th scope="col">Name</th><th scope="col">Key</th><th scope="col">Grant</th><th scope="col">Created</th><th scope="col">Last used</th><th scope="col">Status</th><th scope="col"><span class="sr-only">Actions</span></th></tr>
          </thead>
          <tbody>
            <tr v-for="k in keys" :key="k.id">
              <td class="truncate" :class="k.revoked_at ? 'text-zinc-400' : 'font-medium text-white'" :title="k.name || undefined">{{ k.name || '(unnamed)' }}</td>
              <td class="mono truncate text-zinc-200">{{ k.prefix }}…</td>
              <td class="mono">{{ k.scope }}</td>
              <td class="mono text-zinc-300" :title="stampTitle(k.created_at)">{{ stamp(k.created_at) }}</td>
              <td class="mono text-zinc-300" :title="k.last_used_at ? stampTitle(k.last_used_at) : undefined">{{ k.last_used_at ? stamp(k.last_used_at) : 'never' }}</td>
              <td>
                <span v-if="k.revoked_at" class="st tone-off" :title="stampTitle(k.revoked_at)"><span class="dot dot-off" aria-hidden="true" />Revoked {{ stamp(k.revoked_at) }}</span>
                <span v-else class="st tone-ok"><span class="dot dot-ok" aria-hidden="true" />Active</span>
              </td>
              <td><div class="row-acts"><button v-if="!k.revoked_at" type="button" class="row-act row-act-danger" :aria-label="`Revoke ${k.name || k.prefix + '…'}`" @click="pending = k">Revoke</button></div></td>
            </tr>
          </tbody>
        </table>
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
