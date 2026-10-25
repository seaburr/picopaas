<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, useId } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import ConfirmDialog from './ConfirmDialog.vue'
import { ApiError } from '../api/client'
import { useApiKeysStore } from '../stores/apiKeys'
import { useToastStore } from '../stores/toast'
import type { ApiKey } from '../types'
import { Ban, Copy } from 'lucide-vue-next'
import IconButton from './IconButton.vue'
import { copyText, stamp, stampTitle } from '../utils/format'
import type { ApiKeyScope } from '../types'

const store = useApiKeysStore()
const toast = useToastStore()
const nameId = useId()
const scopeId = useId()
const loading = ref(true)
const error = ref('')
const creating = ref(false)
const newName = ref('')
const newScope = ref<ApiKeyScope>('full')
const showRevoked = ref(false)

// Set only right after a successful create: the raw key is never returned by the API again, so once the
// viewer navigates away or creates another key, it is gone for good.
const justCreatedKey = ref('')
const copied = ref(false)
const tab = ref<'env' | 'cli' | 'curl'>('env')

const pendingRevoke = ref<ApiKey | null>(null)
const active = computed(() => store.keys.filter((k) => !k.revoked_at))
const revoked = computed(() => store.keys.filter((k) => k.revoked_at))
const origin = window.location.origin

const snippets = computed(() => {
  const k = justCreatedKey.value
  return {
    env: `export PICOPAAS_TOKEN=${k}\nexport PICOPAAS_URL=${origin}`,
    cli: `export PICOPAAS_TOKEN=${k}\npython cli/picopaas.py apps`,
    curl: `curl -H "Authorization: Bearer ${k}" ${origin}/apps`,
  }[tab.value]
})
const tabs = [
  { id: 'env', label: 'Environment' },
  { id: 'cli', label: 'CLI' },
  { id: 'curl', label: 'curl' },
] as const

onMounted(async () => {
  try {
    await store.load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load your API keys. Reload to try again."
  } finally {
    loading.value = false
  }
})

// An uncopied one-time key is lost if the user leaves: warn on route change and on tab close.
const unsaved = () => justCreatedKey.value !== '' && !copied.value
onBeforeRouteLeave(() => {
  if (unsaved()) return window.confirm("You haven't copied your new API key. It can't be shown again. Leave anyway?")
})
const beforeUnload = (e: BeforeUnloadEvent) => {
  if (unsaved()) e.preventDefault()
}
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))

async function onCreate() {
  if (unsaved() && !window.confirm("You haven't copied the previous key. It can't be shown again. Create another anyway?")) return
  error.value = ''
  creating.value = true
  copied.value = false
  try {
    const created = await store.create(newName.value.trim() || undefined, newScope.value)
    justCreatedKey.value = created.key
    newName.value = ''
    newScope.value = 'full'
    tab.value = 'env'
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't create the API key. Try again."
  } finally {
    creating.value = false
  }
}

async function copy(text: string, markKey = false) {
  if (await copyText(text)) {
    if (markKey) copied.value = true
    toast.success('Copied.', 2000)
  } else toast.error("Couldn't copy. Select the text and copy it manually.")
}

function dismissCreated() {
  if (unsaved() && !window.confirm("You haven't copied your new API key. It can't be shown again. Dismiss anyway?")) return
  justCreatedKey.value = ''
  copied.value = false
}

async function confirmRevoke() {
  const k = pendingRevoke.value
  if (!k) return
  try {
    await store.revoke(k.id)
    toast.success(`Revoked ${k.name || k.prefix + '…'}.`)
  } catch (e) {
    toast.error(`Couldn't revoke the key: ${e instanceof ApiError ? e.message : 'try again'}`)
  } finally {
    pendingRevoke.value = null
  }
}

// Header and rows share one fixed template (md+); stacked below md.
const keyGrid = 'md:grid-cols-[minmax(0,1fr)_8rem_5rem_8.5rem_9rem_2rem] md:gap-3'
const scopeLabel = (s?: ApiKeyScope) => (s === 'read' ? 'Read-only' : 'Full')
</script>

<template>
  <section id="api-keys" class="card card-pad scroll-mt-4" aria-labelledby="keys-title">
    <h2 id="keys-title" class="section-title">API keys</h2>
    <p class="mt-1 text-sm text-zinc-300">
        API keys let the <RouterLink to="/guide/cli" class="link">picopaas CLI</RouterLink> and scripts deploy for
        you (<RouterLink to="/guide/api" class="link">API reference</RouterLink>). Set
        <code class="mono">PICOPAAS_TOKEN</code> to a key and run
        <code class="mono">picopaas apply -f app.yaml</code>. Choose <strong>Full</strong> (deploy and manage apps, like
        your account) or <strong>Read-only</strong> (list and inspect only — safe for dashboards and CI status checks).
        API keys can't create or revoke keys; do that here.
      </p>

      <form class="mt-3 flex flex-col gap-2 sm:flex-row sm:items-end" @submit.prevent="onCreate">
        <div class="sm:w-60">
          <label :for="nameId" class="label">Name <span class="font-normal text-zinc-400">(optional)</span></label>
          <input :id="nameId" v-model="newName" maxlength="64" placeholder="e.g. github-actions" class="input" autocomplete="off" />
        </div>
        <div class="sm:w-40">
          <label :for="scopeId" class="label">Access</label>
          <select :id="scopeId" v-model="newScope" class="input">
            <option value="full">Full</option>
            <option value="read">Read-only</option>
          </select>
        </div>
        <button type="submit" :disabled="creating" class="btn-primary">{{ creating ? 'Creating…' : 'Create key' }}</button>
      </form>

      <section v-if="justCreatedKey" class="alert-info mt-3 p-3" aria-label="New API key">
        <p class="text-sm font-medium text-white">Copy your new key now. For security we can't show it again.</p>
        <div class="mt-2 flex items-stretch gap-2">
          <code class="mono min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded bg-pico-card px-3 py-1.5 text-accent-light">{{ justCreatedKey }}</code>
          <IconButton :icon="Copy" :label="copied ? 'Copied' : 'Copy key'" @click="copy(justCreatedKey, true)" />
        </div>
        <div class="mt-3" role="tablist" aria-label="Usage snippets">
          <button
            v-for="t in tabs"
            :key="t.id"
            type="button"
            role="tab"
            :aria-selected="tab === t.id"
            class="border-b-2 px-3 py-1.5 text-sm"
            :class="tab === t.id ? 'border-accent-light font-medium text-white' : 'border-transparent text-zinc-300 hover:text-white'"
            @click="tab = t.id"
          >{{ t.label }}</button>
        </div>
        <div class="relative" role="tabpanel">
          <pre class="mono whitespace-pre-wrap break-all rounded-b bg-pico-card px-3 py-2 pr-12 text-zinc-200"><code>{{ snippets }}</code></pre>
          <IconButton :icon="Copy" label="Copy snippet" sm class="absolute right-2 top-2" @click="copy(snippets)" />
        </div>
        <button type="button" class="link mt-3 text-xs" @click="dismissCreated">I've saved it, dismiss</button>
      </section>

      <div v-if="error" class="alert-error mt-3" role="alert">{{ error }}</div>

      <div v-if="loading" class="mt-4 text-sm text-zinc-400">Loading…</div>

      <div v-else-if="store.keys.length === 0" class="card mt-4 p-6 text-center">
        <h2 class="section-title">No API keys yet</h2>
        <p class="mx-auto mt-1 max-w-sm text-sm text-zinc-300">Create one to deploy from the CLI or CI. Keys are shown once, so copy it when it appears.</p>
      </div>

      <template v-else>
        <h2 class="label mt-4">Active keys <span class="meta font-normal">({{ active.length }})</span></h2>
        <p v-if="active.length === 0" class="mt-2 text-sm text-zinc-400">No active keys.</p>
        <div v-else class="card mt-2 overflow-hidden">
          <div :class="[keyGrid, 'table-th row-pad hidden border-b border-pico-border md:grid']">
            <span>Name</span><span>Key</span><span>Access</span><span>Created</span><span>Last used</span><span class="sr-only">Actions</span>
          </div>
          <ul class="divide-y divide-pico-border">
            <li v-for="key in active" :key="key.id" :class="[keyGrid, 'row-pad flex flex-col md:grid md:items-center']">
              <span class="truncate font-medium text-white" :title="key.name || undefined"><span v-if="key.name">{{ key.name }}</span><span v-else class="text-zinc-400">(unnamed)</span></span>
              <code class="mono truncate text-zinc-200" :title="`${key.prefix}…`">{{ key.prefix }}…</code>
              <span><span class="meta md:hidden">Access </span><span class="rounded px-1.5 py-0.5 text-xs font-medium" :class="key.scope === 'read' ? 'bg-pico-card text-zinc-300' : 'bg-accent-light/15 text-accent-light'">{{ scopeLabel(key.scope) }}</span></span>
              <span class="mono text-zinc-300" :title="stampTitle(key.created_at)"><span class="meta md:hidden">Created </span>{{ stamp(key.created_at) }}</span>
              <span class="mono text-zinc-300" :title="key.last_used_at ? stampTitle(key.last_used_at) : undefined"><span class="meta md:hidden">Last used </span>{{ key.last_used_at ? stamp(key.last_used_at) : 'never' }}</span>
              <span class="flex justify-end"><IconButton :icon="Ban" label="Revoke key" danger @click="pendingRevoke = key" /></span>
            </li>
          </ul>
        </div>

        <template v-if="revoked.length">
          <button type="button" class="link mt-4 text-sm" :aria-expanded="showRevoked" @click="showRevoked = !showRevoked">
            {{ showRevoked ? 'Hide' : 'Show' }} {{ revoked.length }} revoked {{ revoked.length === 1 ? 'key' : 'keys' }}
          </button>
          <div v-if="showRevoked" class="card mt-2 overflow-hidden text-zinc-400">
            <ul class="divide-y divide-pico-border">
              <li v-for="key in revoked" :key="key.id" :class="[keyGrid, 'row-pad flex flex-col md:grid md:items-center']">
                <span class="truncate" :title="key.name || undefined">{{ key.name || '(unnamed)' }}</span>
                <code class="mono truncate" :title="`${key.prefix}…`">{{ key.prefix }}…</code>
                <span><span class="meta md:hidden">Access </span>{{ scopeLabel(key.scope) }}</span>
                <span class="mono" :title="stampTitle(key.created_at)"><span class="meta md:hidden">Created </span>{{ stamp(key.created_at) }}</span>
                <span class="mono" :title="stampTitle(key.revoked_at!)">Revoked {{ stamp(key.revoked_at!) }}</span>
                <span />
              </li>
            </ul>
          </div>
        </template>
      </template>

    <ConfirmDialog
      :open="pendingRevoke !== null"
      title="Revoke API key"
      :message="`Revoke ${pendingRevoke?.name || pendingRevoke?.prefix + '…'}? Anything using it will stop working immediately. This can't be undone.`"
      confirm-label="Revoke key"
      danger
      @confirm="confirmRevoke"
      @cancel="pendingRevoke = null"
    />
  </section>
</template>
