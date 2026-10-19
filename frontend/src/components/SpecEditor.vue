<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { applySpec } from '../api/apps'
import { ApiError } from '../api/client'

const router = useRouter()

const spec = ref(`apiVersion: picopaas/v1
name: my-app              # display name only; hostname is generated
image: ghcr.io/me/app:1.2
port: 8080
# healthCheck: { enabled: true, path: /healthz }   # opt-in; default off
resources: { db: true, storage: false }
env:
  LOG_LEVEL: info
  DATABASE_URL: \${{ db.url }}
secrets:                  # values supplied below, never in the file
  - API_TOKEN
`)
const secrets = ref<{ key: string; value: string }[]>([{ key: '', value: '' }])
const error = ref('')
const result = ref('')
const busy = ref(false)

async function apply() {
  error.value = ''
  result.value = ''
  busy.value = true
  try {
    const map: Record<string, string> = {}
    for (const s of secrets.value) if (s.key.trim()) map[s.key.trim()] = s.value
    const app = await applySpec(spec.value, map)
    result.value = `${app.created ? 'Created' : 'Updated'} ${app.name}`
    await router.push('/apps')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Failed to apply spec'
  } finally {
    busy.value = false
  }
}

const input =
  'rounded-md border border-pico-border bg-pico-card px-2 py-1 text-sm text-white focus:outline-none focus:border-accent'
</script>

<template>
  <form class="space-y-4 rounded-lg border border-pico-border bg-pico-panel p-5" @submit.prevent="apply">
    <textarea v-model="spec" rows="14" spellcheck="false" :class="[input, 'w-full font-mono']" />

    <div>
      <label class="mb-1 block text-sm font-medium text-zinc-300">Secrets</label>
      <div v-for="(s, i) in secrets" :key="i" class="mb-1 flex gap-2">
        <input v-model="s.key" placeholder="KEY" autocomplete="off" :class="[input, 'w-48 font-mono']" />
        <input
          v-model="s.value"
          type="password"
          placeholder="value"
          autocomplete="new-password"
          :class="[input, 'flex-1 font-mono']"
        />
        <button
          type="button"
          class="rounded border border-pico-border px-2 text-xs text-danger hover:bg-danger/10"
          @click="secrets.splice(i, 1)"
        >
          Remove
        </button>
      </div>
      <button
        type="button"
        class="rounded border border-pico-border px-2 py-1 text-xs text-zinc-300 hover:bg-pico-card"
        @click="secrets.push({ key: '', value: '' })"
      >
        + Add secret
      </button>
    </div>

    <pre v-if="error" class="whitespace-pre-wrap rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">{{ error }}</pre>
    <div v-if="result" class="text-sm text-zinc-300">{{ result }}</div>

    <div class="flex justify-end">
      <button
        type="submit"
        :disabled="busy"
        class="rounded-md bg-accent-dark px-4 py-2 text-sm font-semibold text-white hover:bg-accent disabled:opacity-50"
      >
        {{ busy ? 'Applying…' : 'Apply' }}
      </button>
    </div>
  </form>
</template>
