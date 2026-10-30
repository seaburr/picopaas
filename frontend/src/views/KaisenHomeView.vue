<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { HeartPulse, Plus, RotateCw, Settings, Trash2, Wand2 } from 'lucide-vue-next'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import HostLink from '../components/HostLink.vue'
import IconButton from '../components/IconButton.vue'
import NavBar from '../components/NavBar.vue'
import PreviewBadge from '../components/PreviewBadge.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError } from '../api/client'
import { relTime, useNow } from '../composables/appUi'
import { fmtUsd, isActive } from '../composables/kaisen'
import { useAuthStore } from '../stores/auth'
import { useKaisenStore } from '../stores/kaisen'
import { useProjectsStore } from '../stores/projects'
import { useToastStore } from '../stores/toast'
import type { KaisenBuild } from '../types'

const store = useKaisenStore()
const projects = useProjectsStore()
const auth = useAuthStore()
const toast = useToastStore()
const now = useNow()
const loading = ref(true)
const error = ref('')

const limit = computed(() => projects.current?.app_limit_effective ?? auth.user?.app_limit_effective ?? auth.user?.app_limit ?? 0)
const atLimit = computed(() => limit.value > 0 && store.builds.filter((b) => b.app_id).length >= limit.value)
const noKey = computed(() => !!store.settings && !store.settings.anthropic_key.set)
const canEdit = computed(() => !!store.settings?.can_edit)
const blocked = computed(() => noKey.value || atLimit.value)
const blockedWhy = computed(() => (noKey.value ? 'Add an Anthropic key in Kaisen settings first' : 'App limit reached. Ask an administrator to raise it.'))

let timer: ReturnType<typeof setInterval> | null = null
function syncPolling() {
  const busy = store.builds.some((b) => isActive(b.status))
  if (busy && !timer) {
    timer = setInterval(async () => {
      try {
        await store.refreshBuilds()
      } catch {
        // keep last known state
      }
    }, 4000)
  } else if (!busy && timer) {
    clearInterval(timer)
    timer = null
  }
}
watch(() => store.builds, syncPolling, { deep: true })
onUnmounted(() => {
  if (timer) clearInterval(timer)
  timer = null
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    if (!projects.loaded) await projects.load()
    await store.load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load your builds."
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch(() => projects.currentId, (n, o) => { if (o !== n && projects.loaded) load() })

const pendingDelete = ref<KaisenBuild | null>(null)
const alsoApp = ref(false)
const deleting = ref(false)
async function confirmDelete() {
  const b = pendingDelete.value
  if (!b) return
  deleting.value = true
  try {
    await store.remove(b.id, alsoApp.value)
    toast.success(`Deleted build ${b.name}.`)
    pendingDelete.value = null
  } catch (e) {
    toast.error(`Couldn't delete ${b.name}: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    deleting.value = false
  }
}
function askDelete(b: KaisenBuild) {
  alsoApp.value = false
  pendingDelete.value = b
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />

    <main id="main" tabindex="-1" class="mx-auto max-w-6xl px-4 py-4 sm:px-6">
      <nav class="meta mb-2" aria-label="Breadcrumb">Kaisen</nav>
      <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 class="page-title">{{ !projects.current ? 'Builds' : projects.current.is_personal ? 'Personal builds' : `Builds in ${projects.current.name}` }}</h1>
          <PreviewBadge />
          <span v-if="!loading && store.builds.length" class="meta">{{ store.builds.length }} {{ store.builds.length === 1 ? 'build' : 'builds' }}</span>
        </div>
        <div class="flex items-center gap-1">
          <span v-if="atLimit && store.builds.length" class="meta mr-2">App limit reached. Ask an administrator to raise it.</span>
          <IconButton :icon="Settings" label="Kaisen settings" to="/kaisen/settings" />
          <template v-if="store.builds.length > 0">
            <IconButton v-if="blocked" :icon="Plus" :label="`New build (${blockedWhy})`" disabled />
            <IconButton v-else :icon="Plus" label="New build" to="/kaisen/new" />
          </template>
        </div>
      </div>

      <div v-if="error" role="alert" class="alert-error mb-4 flex flex-wrap items-center justify-between gap-2">
        <span>{{ error }}</span>
        <IconButton :icon="RotateCw" label="Retry" @click="load" />
      </div>

      <ul v-if="loading" class="space-y-2" aria-busy="true" aria-label="Loading builds">
        <li v-for="n in 3" :key="n" class="card card-pad">
          <div class="h-4 w-40 animate-pulse rounded-sm bg-pico-card" />
          <div class="mt-3 h-3 w-64 max-w-full animate-pulse rounded-sm bg-pico-card" />
        </li>
      </ul>

      <div v-else-if="!error && store.builds.length === 0" class="card card-pad">
        <h2 class="section-title">Build your first app with Kaisen <PreviewBadge /></h2>
        <p class="mt-1 text-sm text-zinc-300">Tell Kaisen what you want in a few sentences. It writes the code, runs it and deploys it as an app in this project. You can download the code or push it to GitHub.</p>
        <p v-if="noKey" class="alert-warn mt-3">
          Kaisen needs an Anthropic API key for this project before it can build.
          <RouterLink v-if="canEdit" to="/kaisen/settings" class="link">Add it in Kaisen settings</RouterLink>
          <template v-else>Ask a project owner to add one.</template>
        </p>
        <div v-else class="mt-5 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
          <span v-if="atLimit" class="meta">App limit reached. Ask an administrator to raise it.</span>
          <RouterLink v-else to="/kaisen/new" class="btn-primary">New build</RouterLink>
          <RouterLink to="/guide/user" class="btn-ghost">Read the guide</RouterLink>
        </div>
      </div>

      <ul v-else-if="store.builds.length" class="space-y-2">
        <li v-for="b in store.builds" :key="b.id" class="card card-pad">
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0 flex-1">
              <div class="flex min-w-0 items-center gap-2">
                <RouterLink :to="`/kaisen/${b.id}`" class="truncate font-medium text-white hover:underline" :title="b.name">{{ b.name }}</RouterLink>
                <StatusBadge kind="build" :status="b.status" />
              </div>
              <div v-if="b.app_hostname" class="mt-0.5"><HostLink :hostname="b.app_hostname" /></div>
              <p class="meta mono">
                <template v-if="b.last_run_at">Last run {{ relTime(b.last_run_at, now) }} · </template>{{ b.file_count }} {{ b.file_count === 1 ? 'file' : 'files' }} · {{ fmtUsd(b.cost_usd) }}
              </p>
            </div>
            <div class="flex shrink-0 items-center">
              <IconButton v-if="b.app_id" :icon="HeartPulse" label="View app health" :to="`/apps/${b.app_id}`" />
              <IconButton :icon="Wand2" label="Open build" :to="`/kaisen/${b.id}`" />
              <IconButton :icon="Trash2" label="Delete build" danger @click="askDelete(b)" />
            </div>
          </div>
          <p v-if="b.status === 'waiting' || b.open_questions > 0" class="alert-info mt-2">Kaisen is waiting for your answer. <RouterLink :to="`/kaisen/${b.id}`" class="link">Open the build</RouterLink></p>
          <p v-if="(b.status === 'error' || b.status === 'stopped') && b.last_error" class="alert-error mt-2 truncate" :title="b.last_error">{{ b.last_error }}</p>
        </li>
      </ul>

      <p v-if="!loading && !error && store.spend" class="help mt-3">
        Kaisen is in preview. Builds spend from this project's Anthropic key. This month: <span class="mono">{{ fmtUsd(store.spend.month_usd) }}</span><template v-if="store.spend.cap_day"> · daily cap <span class="mono">{{ fmtUsd(store.spend.cap_day) }}</span> (today <span class="mono">{{ fmtUsd(store.spend.today_usd) }}</span>)</template>
      </p>
    </main>

    <ConfirmDialog
      :open="!!pendingDelete"
      title="Delete build?"
      message="This removes the build, its files and run history. Spend totals stay in your usage."
      danger
      confirm-label="Delete build"
      :require-text="pendingDelete?.name"
      :busy="deleting"
      busy-label="Deleting…"
      @confirm="confirmDelete"
      @cancel="pendingDelete = null"
    >
      <label v-if="pendingDelete?.app_id" class="mt-3 flex items-center gap-2 text-sm text-zinc-200">
        <input v-model="alsoApp" type="checkbox" class="check" /> Also delete the app ({{ pendingDelete.name }})
      </label>
    </ConfirmDialog>
  </div>
</template>
