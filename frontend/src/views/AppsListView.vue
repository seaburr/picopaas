<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import AppStatusNote from '../components/AppStatusNote.vue'
import DeleteAppDialog from '../components/DeleteAppDialog.vue'
import HostLink from '../components/HostLink.vue'
import NavBar from '../components/NavBar.vue'
import IconButton from '../components/IconButton.vue'
import { Pencil, RotateCw, ScrollText, Trash2 } from 'lucide-vue-next'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError } from '../api/client'
import { SAMPLE_APP } from '../composables/appUi'
import { useAppsStore } from '../stores/apps'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'
import type { App } from '../types'

const store = useAppsStore()
const auth = useAuthStore()
const toast = useToastStore()
const router = useRouter()
const loading = ref(true)
const error = ref('')
const restarting = ref<number | null>(null)

const limit = computed(() => auth.user?.app_limit ?? 0)
const atLimit = computed(() => limit.value > 0 && store.apps.length >= limit.value)

// Poll while anything is queued or deploying.
let timer: ReturnType<typeof setInterval> | null = null
function inFlight() {
  return store.apps.some((a) => a.status === 'pending' || a.status === 'deploying')
}
function syncPolling() {
  if (inFlight() && !timer) {
    timer = setInterval(async () => {
      try {
        await store.load()
      } catch {
        // keep last known state; next tick retries
      }
    }, 4000)
  } else if (!inFlight() && timer) {
    clearInterval(timer)
    timer = null
  }
}
watch(() => store.apps, syncPolling, { deep: true })
onUnmounted(() => {
  if (timer) clearInterval(timer)
  timer = null
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    await store.load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load your apps."
  } finally {
    loading.value = false
  }
}
onMounted(load)

const pendingDelete = ref<App | null>(null)
const deleting = ref(false)
async function confirmDelete() {
  const app = pendingDelete.value
  if (!app) return
  deleting.value = true
  try {
    await store.remove(app.id)
    toast.success(`Deleted ${app.name}.`)
    pendingDelete.value = null
  } catch (e) {
    toast.error(`Couldn't delete ${app.name}: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    deleting.value = false
  }
}

async function restart(app: App) {
  restarting.value = app.id
  try {
    await store.restart(app.id)
    toast.success(`Restarting ${app.name}.`)
  } catch (e) {
    toast.error(
      e instanceof ApiError && e.status === 404
        ? "Restart isn't available on this platform version yet."
        : `Couldn't restart ${app.name}: ${e instanceof ApiError ? e.message : 'unknown error'}`,
    )
  } finally {
    restarting.value = null
  }
}

function trySample() {
  router.push({ path: '/apps/new', query: { sample: '1' } })
}

function meta(app: App) {
  const bits = [app.image, `port ${app.port}`]
  const extras = [app.want_db ? 'database' : '', app.want_storage ? 'storage' : ''].filter(Boolean)
  if (extras.length) bits.push(extras.join(' + '))
  return bits
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />

    <main id="main" tabindex="-1" class="mx-auto max-w-5xl px-4 py-4 sm:px-6">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div class="flex items-baseline gap-3">
          <h1 class="page-title">Apps</h1>
          <span v-if="!loading && limit" class="meta" :title="`You can have up to ${limit} apps`">{{ store.apps.length }} / {{ limit }}</span>
        </div>
        <div v-if="store.apps.length > 0" class="flex items-center gap-3">
          <span v-if="atLimit" class="meta">App limit reached. Ask an administrator to raise it.</span>
          <button v-if="atLimit" type="button" class="btn-primary" disabled>New app</button>
          <RouterLink v-else to="/apps/new" class="btn-primary">New app</RouterLink>
        </div>
      </div>

      <div v-if="error" role="alert" class="alert-error mb-4 flex flex-wrap items-center justify-between gap-2">
        <span>{{ error }}</span>
        <button type="button" class="btn-secondary btn-sm" @click="load">Retry</button>
      </div>

      <ul v-if="loading" class="space-y-2" aria-busy="true" aria-label="Loading apps">
        <li v-for="n in 3" :key="n" class="card card-pad">
          <div class="h-4 w-40 animate-pulse rounded bg-pico-card" />
          <div class="mt-3 h-3 w-64 max-w-full animate-pulse rounded bg-pico-card" />
        </li>
      </ul>

      <div v-else-if="!error && store.apps.length === 0" class="card card-pad">
        <h2 class="section-title">Deploy your first app</h2>
        <p class="mt-1 text-sm text-zinc-300">Three things and you're live:</p>
        <ol class="mt-3 list-decimal space-y-1 pl-5 text-sm text-zinc-300">
          <li>A public Docker image that runs as a non-root user.</li>
          <li>The port it listens on.</li>
          <li>We give you an https URL.</li>
        </ol>
        <div class="mt-5 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
          <RouterLink to="/apps/new" class="btn-primary">Create app</RouterLink>
          <button type="button" class="btn-secondary" @click="trySample">Try a sample app</button>
          <RouterLink to="/guide/user" class="btn-ghost">Read the guide</RouterLink>
        </div>
        <p class="help">The sample deploys podinfo ({{ SAMPLE_APP.image }}, port {{ SAMPLE_APP.port }}), a tiny demo web app.</p>
      </div>

      <ul v-else-if="store.apps.length" class="space-y-2">
        <li v-for="app in store.apps" :key="app.id" class="card card-pad">
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0 flex-1">
              <div class="flex min-w-0 items-center gap-2">
                <RouterLink :to="`/apps/${app.id}`" class="truncate font-medium text-white hover:underline" :title="app.name">{{ app.name }}</RouterLink>
                <StatusBadge :status="app.status" />
              </div>
              <div class="mt-0.5"><HostLink :hostname="app.hostname" /></div>
              <p class="meta mono truncate" :title="meta(app).join(' · ')">
                <template v-for="(b, i) in meta(app)" :key="i"><span v-if="i" aria-hidden="true"> · </span>{{ b }}</template>
              </p>
            </div>
            <div class="flex shrink-0 items-center">
              <IconButton :icon="ScrollText" label="View logs" :to="`/apps/${app.id}#logs`" />
              <IconButton :icon="RotateCw" :label="app.status === 'suspended' ? 'Restart (unavailable while suspended)' : 'Restart'" :disabled="app.status === 'suspended' || restarting === app.id" @click="restart(app)" />
              <IconButton
                :icon="Pencil"
                :label="app.status === 'suspended' ? 'Edit (suspended: contact the administrator)' : 'Edit'"
                :to="`/apps/${app.id}/edit`"
                :disabled="app.status === 'suspended'"
              />
              <IconButton :icon="Trash2" label="Delete" danger @click="pendingDelete = app" />
            </div>
          </div>
          <div v-if="['suspended', 'pending', 'deploying', 'error'].includes(app.status)" class="mt-2 border-t border-pico-border pt-2">
            <AppStatusNote compact :app="app" :restarting="restarting === app.id" @restart="restart(app)" />
          </div>
        </li>
      </ul>
    </main>

    <DeleteAppDialog :app="pendingDelete" :busy="deleting" @confirm="confirmDelete" @cancel="pendingDelete = null" />
  </div>
</template>
