<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, useId } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import NavBar from '../components/NavBar.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError } from '../api/client'
import { useApiKeysStore } from '../stores/apiKeys'
import { useToastStore } from '../stores/toast'
import type { ApiKey } from '../types'
import { copyText, fullDate, humanDate } from '../utils/format'

const store = useApiKeysStore()
const toast = useToastStore()
const nameId = useId()
const loading = ref(true)
const error = ref('')
const creating = ref(false)
const newName = ref('')
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
    const created = await store.create(newName.value.trim() || undefined)
    justCreatedKey.value = created.key
    newName.value = ''
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
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />

    <main id="main" tabindex="-1" class="mx-auto max-w-5xl px-4 py-6 sm:px-6">
      <h1 class="page-title">API keys</h1>
      <p class="mt-1 max-w-2xl text-sm text-zinc-300">
        API keys let the <RouterLink to="/guide/cli" class="link">picopaas CLI</RouterLink> and scripts deploy for
        you (<RouterLink to="/guide/api" class="link">API reference</RouterLink>). Set
        <code class="font-mono text-xs">PICOPAAS_TOKEN</code> to a key and run
        <code class="font-mono text-xs">picopaas apply -f app.yaml</code>. A key has the same access as your account.
      </p>

      <form class="mt-5 flex flex-col gap-2 sm:flex-row sm:items-end" @submit.prevent="onCreate">
        <div class="sm:w-72">
          <label :for="nameId" class="label">Name <span class="font-normal text-zinc-400">(optional)</span></label>
          <input :id="nameId" v-model="newName" maxlength="60" placeholder="e.g. github-actions" class="input" autocomplete="off" />
        </div>
        <button type="submit" :disabled="creating" class="btn-primary">{{ creating ? 'Creating…' : 'Create API key' }}</button>
      </form>

      <section v-if="justCreatedKey" class="alert-info mt-5 p-4" aria-label="New API key">
        <p class="text-sm font-medium text-white">Copy your new key now. For security we can't show it again.</p>
        <div class="mt-2 flex items-stretch gap-2">
          <code class="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded bg-pico-card px-3 py-2 text-sm text-accent-light">{{ justCreatedKey }}</code>
          <button type="button" class="btn-primary shrink-0" @click="copy(justCreatedKey, true)">{{ copied ? 'Copied' : 'Copy' }}</button>
        </div>
        <div class="mt-4" role="tablist" aria-label="Usage snippets">
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
          <pre class="whitespace-pre-wrap break-all rounded-b bg-pico-card px-3 py-3 pr-16 text-xs text-zinc-200"><code>{{ snippets }}</code></pre>
          <button type="button" class="btn-secondary btn-sm absolute right-2 top-2" @click="copy(snippets)">Copy</button>
        </div>
        <button type="button" class="link mt-3 text-xs" @click="dismissCreated">I've saved it, dismiss</button>
      </section>

      <div v-if="error" class="alert-error mt-5" role="alert">{{ error }}</div>

      <div v-if="loading" class="mt-6 text-sm text-zinc-400">Loading…</div>

      <div v-else-if="store.keys.length === 0" class="card mt-6 p-8 text-center">
        <h2 class="section-title">No API keys yet</h2>
        <p class="mx-auto mt-1 max-w-sm text-sm text-zinc-300">Create one to deploy from the CLI or CI. Keys are shown once, so copy it when it appears.</p>
      </div>

      <template v-else>
        <h2 class="section-title mt-8">Active keys <span class="meta font-normal">({{ active.length }})</span></h2>
        <p v-if="active.length === 0" class="mt-2 text-sm text-zinc-400">No active keys.</p>
        <ul v-else class="mt-2 space-y-2">
          <li v-for="key in active" :key="key.id" class="card flex flex-wrap items-center justify-between gap-3 p-4">
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <span v-if="key.name" class="font-medium text-white">{{ key.name }}</span>
                <code class="font-mono text-sm" :class="key.name ? 'text-zinc-300' : 'text-white'">{{ key.prefix }}…</code>
                <StatusBadge status="active" />
              </div>
              <p class="meta mt-1">
                <span :title="fullDate(key.created_at)">Created {{ humanDate(key.created_at) }}</span>
                <template v-if="key.last_used_at"> · <span :title="fullDate(key.last_used_at)">Last used {{ humanDate(key.last_used_at) }}</span></template>
              </p>
            </div>
            <button type="button" class="btn-danger btn-sm" @click="pendingRevoke = key">Revoke</button>
          </li>
        </ul>

        <template v-if="revoked.length">
          <button type="button" class="link mt-6 text-sm" :aria-expanded="showRevoked" @click="showRevoked = !showRevoked">
            {{ showRevoked ? 'Hide' : 'Show' }} {{ revoked.length }} revoked {{ revoked.length === 1 ? 'key' : 'keys' }}
          </button>
          <ul v-if="showRevoked" class="mt-2 space-y-2">
            <li v-for="key in revoked" :key="key.id" class="card flex flex-wrap items-center gap-2 p-3 text-zinc-300">
              <span v-if="key.name">{{ key.name }}</span>
              <code class="font-mono text-sm">{{ key.prefix }}…</code>
              <StatusBadge status="revoked" />
              <span class="meta" :title="fullDate(key.revoked_at!)">Revoked {{ humanDate(key.revoked_at!) }}</span>
            </li>
          </ul>
        </template>
      </template>
    </main>

    <ConfirmDialog
      :open="pendingRevoke !== null"
      title="Revoke API key"
      :message="`Revoke ${pendingRevoke?.name || pendingRevoke?.prefix + '…'}? Anything using it will stop working immediately. This can't be undone.`"
      confirm-label="Revoke key"
      danger
      @confirm="confirmRevoke"
      @cancel="pendingRevoke = null"
    />
  </div>
</template>
