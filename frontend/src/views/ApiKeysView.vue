<script setup lang="ts">
import { onMounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import NavBar from '../components/NavBar.vue'
import { ApiError } from '../api/client'
import { useApiKeysStore } from '../stores/apiKeys'
import type { ApiKey } from '../types'

const store = useApiKeysStore()
const loading = ref(true)
const error = ref('')
const creating = ref(false)

// Set only right after a successful create — the raw key is never returned
// by the API again (app/routers/api_keys.py), so once the viewer navigates
// away or creates another key, this is gone for good.
const justCreatedKey = ref('')
const copied = ref(false)

const pendingRevoke = ref<ApiKey | null>(null)

onMounted(async () => {
  try {
    await store.load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Failed to load API keys'
  } finally {
    loading.value = false
  }
})

async function onCreate() {
  error.value = ''
  creating.value = true
  copied.value = false
  try {
    const created = await store.create()
    justCreatedKey.value = created.key
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Failed to create API key'
  } finally {
    creating.value = false
  }
}

async function copyKey() {
  try {
    await navigator.clipboard.writeText(justCreatedKey.value)
    copied.value = true
  } catch {
    // Clipboard API can be unavailable (permissions, non-secure context) —
    // the key is still shown in the box for manual copy.
  }
}

function dismissCreated() {
  justCreatedKey.value = ''
  copied.value = false
}

function askRevoke(key: ApiKey) {
  pendingRevoke.value = key
}

async function confirmRevoke() {
  if (!pendingRevoke.value) return
  try {
    await store.revoke(pendingRevoke.value.id)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Failed to revoke API key'
  } finally {
    pendingRevoke.value = null
  }
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleString()
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />

    <main class="mx-auto max-w-3xl px-4 py-6">
      <div class="mb-4 flex items-center justify-between">
        <h1 class="text-lg font-semibold text-white">API keys</h1>
        <button
          type="button"
          :disabled="creating"
          class="rounded-md bg-accent-dark px-3 py-1.5 text-sm font-semibold text-white hover:bg-accent disabled:opacity-50"
          @click="onCreate"
        >
          {{ creating ? 'Creating…' : '+ New key' }}
        </button>
      </div>

      <p class="mb-4 text-sm text-zinc-500">
        Use an API key as a Bearer token (<code class="font-mono text-xs">pp_sk_...</code>) for CLI or YAML-based
        access, instead of logging in with a cookie session.
      </p>

      <div v-if="justCreatedKey" class="mb-4 rounded-md border border-accent/40 bg-accent/10 p-4">
        <p class="text-sm font-medium text-white">
          Your new API key — copy it now, you won't see it again.
        </p>
        <div class="mt-2 flex items-center gap-2">
          <code class="flex-1 overflow-x-auto rounded bg-pico-card px-3 py-2 text-sm text-accent-light">{{
            justCreatedKey
          }}</code>
          <button
            type="button"
            class="shrink-0 rounded border border-pico-border px-3 py-2 text-sm text-zinc-300 hover:bg-pico-card"
            @click="copyKey"
          >
            {{ copied ? 'Copied' : 'Copy' }}
          </button>
        </div>
        <button type="button" class="mt-3 text-xs text-zinc-500 hover:underline" @click="dismissCreated">
          Dismiss
        </button>
      </div>

      <div v-if="error" class="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
        {{ error }}
      </div>

      <div v-if="loading" class="text-sm text-zinc-500">Loading…</div>

      <div
        v-else-if="store.keys.length === 0"
        class="rounded-md border border-pico-border bg-pico-panel p-6 text-center text-sm text-zinc-500"
      >
        No API keys yet.
      </div>

      <ul v-else class="space-y-2">
        <li
          v-for="key in store.keys"
          :key="key.id"
          class="flex items-center justify-between rounded-md border border-pico-border bg-pico-panel p-4"
        >
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <code class="font-mono text-sm text-white">{{ key.prefix }}…</code>
              <span
                v-if="key.revoked_at"
                class="inline-block rounded-full border border-pico-muted/30 bg-pico-muted/15 px-2 py-0.5 text-xs font-medium text-zinc-300"
              >
                Revoked
              </span>
              <span
                v-else
                class="inline-block rounded-full border border-status-running/30 bg-status-running/15 px-2 py-0.5 text-xs font-medium text-status-running"
              >
                Active
              </span>
            </div>
            <p class="text-xs text-zinc-500">
              Created {{ formatDate(key.created_at) }}
              <template v-if="key.revoked_at"> · Revoked {{ formatDate(key.revoked_at) }}</template>
            </p>
          </div>
          <button
            v-if="!key.revoked_at"
            type="button"
            class="shrink-0 rounded border border-pico-border px-2 py-1 text-xs text-danger hover:bg-danger/10"
            @click="askRevoke(key)"
          >
            Revoke
          </button>
        </li>
      </ul>
    </main>

    <ConfirmDialog
      :open="pendingRevoke !== null"
      title="Revoke API key"
      :message="`Revoke key '${pendingRevoke?.prefix}…'? Anything using it will stop working immediately.`"
      confirm-label="Revoke"
      danger
      @confirm="confirmRevoke"
      @cancel="pendingRevoke = null"
    />
  </div>
</template>
