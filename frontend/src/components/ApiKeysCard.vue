<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, useId } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import ConfirmDialog from './ConfirmDialog.vue'
import { ApiError } from '../api/client'
import { useApiKeysStore } from '../stores/apiKeys'
import { useToastStore } from '../stores/toast'
import type { ApiKey } from '../types'
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

const scopeLabel = (s?: ApiKeyScope) => (s === 'read' ? 'Read-only' : 'Full')
</script>

<template>
  <section id="api-keys" class="sec scroll-mt-4 pt-4" aria-labelledby="keys-title">
    <h2 id="keys-title" class="section-title">API keys</h2>
    <p class="mt-1 max-w-3xl text-sm text-zinc-300">
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
        <code class="mono min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded-sm bg-pico-card px-3 py-1.5 text-white">{{ justCreatedKey }}</code>
        <IconButton text="Copy" :label="copied ? 'Copied' : 'Copy key'" @click="copy(justCreatedKey, true)" />
      </div>
      <div class="mt-3" role="tablist" aria-label="Usage snippets">
        <button
          v-for="t in tabs"
          :key="t.id"
          type="button"
          role="tab"
          :aria-selected="tab === t.id"
          class="border-b-2 px-3 py-1.5 text-sm max-md:min-h-[36px]"
          :class="tab === t.id ? 'border-white font-medium text-white' : 'border-transparent text-zinc-300 hover:text-white'"
          @click="tab = t.id"
        >{{ t.label }}</button>
      </div>
      <div class="relative" role="tabpanel">
        <pre class="mono whitespace-pre-wrap break-all rounded-b bg-pico-card px-3 py-2 pr-16 text-zinc-200"><code>{{ snippets }}</code></pre>
        <IconButton text="Copy" label="Copy snippet" class="absolute right-2 top-2" @click="copy(snippets)" />
      </div>
      <button type="button" class="link mt-3 text-xs max-md:min-h-[36px]" @click="dismissCreated">I've saved it, dismiss</button>
    </section>

    <div v-if="error" class="alert-error mt-3" role="alert">{{ error }}</div>

    <div v-if="loading" class="mt-4 text-sm text-zinc-400">Loading…</div>

    <div v-else-if="store.keys.length === 0" class="mt-4">
      <h3 class="text-sm font-medium text-white">No API keys yet</h3>
      <p class="mt-1 max-w-md text-sm text-zinc-300">Create one to deploy from the CLI or CI. Keys are shown once, so copy it when it appears.</p>
    </div>

    <template v-else>
      <h3 class="label mt-4">Active keys <span class="meta font-normal">({{ active.length }})</span></h3>
      <p v-if="active.length === 0" class="mt-2 text-sm text-zinc-400">No active keys.</p>
      <template v-else>
        <table class="tbl mt-1 max-[759px]:hidden">
          <colgroup>
            <col />
            <col class="w-36" />
            <col class="w-24" />
            <col class="w-40" />
            <col class="w-40" />
            <col class="w-20" />
          </colgroup>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Key</th>
              <th scope="col">Access</th>
              <th scope="col">Created</th>
              <th scope="col">Last used</th>
              <th scope="col" class="text-right"><span class="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="key in active" :key="key.id">
              <td class="truncate font-medium text-white" :title="key.name || undefined"><span v-if="key.name">{{ key.name }}</span><span v-else class="font-normal text-zinc-400">(unnamed)</span></td>
              <td class="mono truncate text-zinc-200" :title="`${key.prefix}…`">{{ key.prefix }}…</td>
              <td class="text-zinc-300">{{ scopeLabel(key.scope) }}</td>
              <td class="mono truncate text-zinc-300" :title="stampTitle(key.created_at)">{{ stamp(key.created_at) }}</td>
              <td class="mono truncate text-zinc-300" :title="key.last_used_at ? stampTitle(key.last_used_at) : undefined">{{ key.last_used_at ? stamp(key.last_used_at) : 'never' }}</td>
              <td class="text-right"><div class="row-acts"><button type="button" class="row-act row-act-danger max-[759px]:!min-h-[44px]" aria-label="Revoke key" @click="pendingRevoke = key">Revoke</button></div></td>
            </tr>
          </tbody>
        </table>
        <ul class="mt-1 divide-y divide-pico-border border-y border-pico-border min-[760px]:hidden">
          <li v-for="key in active" :key="key.id" class="py-2">
            <div class="flex items-baseline justify-between gap-2">
              <span class="min-w-0 truncate font-medium text-white"><span v-if="key.name">{{ key.name }}</span><span v-else class="font-normal text-zinc-400">(unnamed)</span></span>
              <code class="mono shrink-0 text-zinc-200">{{ key.prefix }}…</code>
            </div>
            <div class="meta">{{ scopeLabel(key.scope) }} · created <span class="mono">{{ stamp(key.created_at) }}</span> · used <span class="mono">{{ key.last_used_at ? stamp(key.last_used_at) : 'never' }}</span></div>
            <div class="row-acts !justify-start"><button type="button" class="row-act row-act-danger max-[759px]:!min-h-[44px]" aria-label="Revoke key" @click="pendingRevoke = key">Revoke</button></div>
          </li>
        </ul>
      </template>

      <template v-if="revoked.length">
        <button type="button" class="link mt-4 text-sm max-md:min-h-[36px]" :aria-expanded="showRevoked" @click="showRevoked = !showRevoked">
          {{ showRevoked ? 'Hide' : 'Show' }} {{ revoked.length }} revoked {{ revoked.length === 1 ? 'key' : 'keys' }}
        </button>
        <div v-if="showRevoked" class="text-zinc-400">
          <table class="tbl mt-1 max-[759px]:hidden">
            <colgroup>
              <col />
              <col class="w-36" />
              <col class="w-24" />
              <col class="w-40" />
              <col class="w-60" />
            </colgroup>
            <thead>
              <tr><th scope="col">Name</th><th scope="col">Key</th><th scope="col">Access</th><th scope="col">Created</th><th scope="col">Revoked</th></tr>
            </thead>
            <tbody>
              <tr v-for="key in revoked" :key="key.id">
                <td class="truncate" :title="key.name || undefined">{{ key.name || '(unnamed)' }}</td>
                <td class="mono truncate" :title="`${key.prefix}…`">{{ key.prefix }}…</td>
                <td>{{ scopeLabel(key.scope) }}</td>
                <td class="mono truncate" :title="stampTitle(key.created_at)">{{ stamp(key.created_at) }}</td>
                <td class="mono truncate" :title="stampTitle(key.revoked_at!)">Revoked {{ stamp(key.revoked_at!) }}</td>
              </tr>
            </tbody>
          </table>
          <ul class="mt-1 divide-y divide-pico-border border-y border-pico-border min-[760px]:hidden">
            <li v-for="key in revoked" :key="key.id" class="py-2 text-sm">
              <div class="flex items-baseline justify-between gap-2">
                <span class="min-w-0 truncate">{{ key.name || '(unnamed)' }}</span>
                <code class="mono shrink-0">{{ key.prefix }}…</code>
              </div>
              <div class="meta">{{ scopeLabel(key.scope) }} · <span class="mono">Revoked {{ stamp(key.revoked_at!) }}</span></div>
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
