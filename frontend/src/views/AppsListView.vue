<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import NavBar from '../components/NavBar.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError } from '../api/client'
import { useAppsStore } from '../stores/apps'
import type { App } from '../types'

const store = useAppsStore()
const loading = ref(true)
const error = ref('')

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
      syncPolling()
    }, 5000)
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

const pendingDelete = ref<App | null>(null)

onMounted(async () => {
  try {
    await store.load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Failed to load apps'
  } finally {
    loading.value = false
  }
})

function askDelete(app: App) {
  pendingDelete.value = app
}

async function confirmDelete() {
  if (!pendingDelete.value) return
  try {
    await store.remove(pendingDelete.value.id)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Failed to delete app'
  } finally {
    pendingDelete.value = null
  }
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />

    <main class="mx-auto max-w-4xl px-4 py-6">
      <div class="mb-4 flex items-center justify-between">
        <h1 class="text-lg font-semibold text-white">Apps</h1>
        <RouterLink
          to="/apps/new"
          class="rounded-md bg-accent-dark px-3 py-1.5 text-sm font-semibold text-white hover:bg-accent"
        >
          + New app
        </RouterLink>
      </div>

      <div v-if="error" class="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
        {{ error }}
      </div>

      <div v-if="loading" class="text-sm text-zinc-500">Loading…</div>

      <div v-else-if="store.apps.length === 0" class="rounded-md border border-pico-border bg-pico-panel p-6 text-center text-sm text-zinc-500">
        No apps yet. Create your first one.
      </div>

      <ul v-else class="space-y-2">
        <li
          v-for="app in store.apps"
          :key="app.id"
          class="flex items-center justify-between rounded-md border border-pico-border bg-pico-panel p-4"
        >
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <span class="font-medium text-white">{{ app.name }}</span>
              <StatusBadge :status="app.status" />
            </div>
            <p v-if="app.status === 'suspended'" class="text-xs text-amber-400">Suspended by an administrator</p>
            <p v-else-if="app.status_detail" class="truncate text-xs text-zinc-400">{{ app.status_detail }}</p>
            <a
              :href="`https://${app.hostname}`"
              target="_blank"
              rel="noopener noreferrer"
              class="block truncate text-sm text-accent-light hover:underline"
            >
              {{ app.hostname }}
            </a>
            <p class="truncate text-xs text-zinc-500">{{ app.image }}</p>
          </div>
          <div class="flex shrink-0 items-center gap-2">
            <span
              v-if="app.status === 'suspended'"
              class="cursor-not-allowed rounded border border-pico-border px-2 py-1 text-xs text-zinc-600"
            >
              Edit
            </span>
            <RouterLink
              v-else
              :to="`/apps/${app.id}/edit`"
              class="rounded border border-pico-border px-2 py-1 text-xs text-zinc-300 hover:bg-pico-card"
            >
              Edit
            </RouterLink>
            <button
              type="button"
              class="rounded border border-pico-border px-2 py-1 text-xs text-danger hover:bg-danger/10"
              @click="askDelete(app)"
            >
              Delete
            </button>
          </div>
        </li>
      </ul>
    </main>

    <ConfirmDialog
      :open="pendingDelete !== null"
      title="Delete app"
      :message="`Delete '${pendingDelete?.name}'? This can't be undone.`"
      confirm-label="Delete"
      danger
      @confirm="confirmDelete"
      @cancel="pendingDelete = null"
    />
  </div>
</template>
