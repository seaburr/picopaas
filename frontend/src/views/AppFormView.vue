<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import EnvVarEditor from '../components/EnvVarEditor.vue'
import NavBar from '../components/NavBar.vue'
import { ApiError } from '../api/client'
import { useAppsStore } from '../stores/apps'
import type { EnvVar } from '../types'

const props = defineProps<{ id?: string }>()
const router = useRouter()
const store = useAppsStore()

const isEdit = computed(() => props.id !== undefined)
const appId = computed(() => (props.id !== undefined ? Number(props.id) : null))

const form = reactive({
  name: '',
  image: '',
  port: 8080,
  want_db: false,
  want_storage: false,
})
const env = ref<EnvVar[]>([])

const loading = ref(isEdit.value)
const saving = ref(false)
const error = ref('')

// Backend 422s for the image field (app/routers/apps.py _check_non_root /
// _check_image_cap) are plain strings that all start with "image" — e.g.
// "image runs as root, which isn't supported...", "image USER '...' must be
// a numeric UID...", "image not found or not public", "image is N bytes,
// over the M byte limit...". Show those verbatim next to the field instead
// of (or in addition to) the generic error banner.
const imageError = computed(() => (error.value.toLowerCase().startsWith('image') ? error.value : ''))
const formError = computed(() => (imageError.value ? '' : error.value))

onMounted(async () => {
  if (!isEdit.value || appId.value === null) return
  try {
    const app = await store.fetchOne(appId.value)
    form.name = app.name
    form.image = app.image
    form.port = app.port
    form.want_db = app.want_db
    form.want_storage = app.want_storage
    env.value = app.env
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Failed to load app'
  } finally {
    loading.value = false
  }
})

async function onSubmit() {
  error.value = ''
  saving.value = true
  try {
    if (isEdit.value && appId.value !== null) {
      await store.update(appId.value, {
        name: form.name,
        image: form.image,
        port: form.port,
        env: env.value,
      })
    } else {
      await store.create({
        name: form.name,
        image: form.image,
        port: form.port,
        want_db: form.want_db,
        want_storage: form.want_storage,
        env: env.value,
      })
    }
    await router.push('/')
  } catch (e) {
    // The backend's own message is already user-friendly, e.g. for the app
    // limit (app/routers/apps.py create_app): "app limit reached (N);
    // contact the uberadmin to raise it".
    error.value = e instanceof ApiError ? e.message : 'Failed to save app'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />

    <main class="mx-auto max-w-2xl px-4 py-6">
      <h1 class="mb-4 text-lg font-semibold text-white">{{ isEdit ? 'Edit app' : 'New app' }}</h1>

      <div v-if="loading" class="text-sm text-zinc-500">Loading…</div>

      <form v-else class="space-y-5 rounded-lg border border-pico-border bg-pico-panel p-5" @submit.prevent="onSubmit">
        <div>
          <label class="mb-1 block text-sm font-medium text-zinc-300">Name</label>
          <input
            v-model="form.name"
            type="text"
            required
            maxlength="255"
            class="w-full rounded-md border border-pico-border bg-pico-card px-3 py-2 text-white placeholder-zinc-500 focus:outline-none focus:border-accent"
          />
        </div>

        <div>
          <label class="mb-1 block text-sm font-medium text-zinc-300">Image</label>
          <input
            v-model="form.image"
            type="text"
            required
            maxlength="512"
            placeholder="ghcr.io/me/app:1.2"
            class="w-full rounded-md border border-pico-border bg-pico-card px-3 py-2 font-mono text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-accent"
          />
          <p class="mt-1 text-xs text-zinc-500">
            Images must run as a non-root numeric user — add <code class="font-mono">USER 10001</code> to your
            Dockerfile.
          </p>
          <p v-if="imageError" class="mt-1 text-xs text-danger">{{ imageError }}</p>
        </div>

        <div>
          <label class="mb-1 block text-sm font-medium text-zinc-300">Port</label>
          <input
            v-model.number="form.port"
            type="number"
            required
            min="1"
            max="65535"
            class="w-32 rounded-md border border-pico-border bg-pico-card px-3 py-2 text-white focus:outline-none focus:border-accent"
          />
        </div>

        <div class="flex gap-6">
          <label class="flex items-center gap-2 text-sm text-zinc-300">
            <input
              v-model="form.want_db"
              type="checkbox"
              :disabled="isEdit"
              class="accent-accent"
            />
            Database
          </label>
          <label class="flex items-center gap-2 text-sm text-zinc-300">
            <input
              v-model="form.want_storage"
              type="checkbox"
              :disabled="isEdit"
              class="accent-accent"
            />
            Storage
          </label>
        </div>
        <p v-if="isEdit" class="text-xs text-zinc-500">
          Database and storage can't be changed after an app is created.
        </p>

        <div>
          <label class="mb-2 block text-sm font-medium text-zinc-300">Env vars</label>
          <EnvVarEditor v-model="env" :want-db="form.want_db" :want-storage="form.want_storage" />
        </div>

        <div v-if="formError" class="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {{ formError }}
        </div>

        <div class="flex justify-end gap-2">
          <RouterLink to="/" class="rounded-md border border-pico-border px-4 py-2 text-sm text-zinc-300 hover:bg-pico-card">
            Cancel
          </RouterLink>
          <button
            type="submit"
            :disabled="saving"
            class="rounded-md bg-accent-dark px-4 py-2 text-sm font-semibold text-white hover:bg-accent disabled:opacity-50"
          >
            {{ saving ? 'Saving…' : isEdit ? 'Save changes' : 'Create app' }}
          </button>
        </div>
      </form>
    </main>
  </div>
</template>
