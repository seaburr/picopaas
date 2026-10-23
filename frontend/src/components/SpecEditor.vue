<script setup lang="ts">
import { Trash2 } from 'lucide-vue-next'
import IconButton from './IconButton.vue'
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { applySpec } from '../api/apps'
import { ApiError } from '../api/client'
import { useToastStore } from '../stores/toast'

const router = useRouter()
const toast = useToastStore()

const EXAMPLE = `apiVersion: picopaas/v1
name: my-app              # display name only; hostname is generated
image: ghcr.io/stefanprodan/podinfo:6.15.0
port: 9898
# healthCheck: { enabled: true, path: /healthz, timeoutSeconds: 3 }   # opt-in; default off; timeout 1-90s, optional
resources: { db: false, storage: false }
env:
  LOG_LEVEL: info
secrets:                  # values are entered below, never in the file
  - API_TOKEN
`

const spec = ref('')
const secrets = ref<{ key: string; value: string }[]>([])
const error = ref('')
const busy = ref(false)

// Secret names listed under `secrets:` get a value row automatically.
function secretNames(text: string): string[] {
  const out: string[] = []
  let inBlock = false
  for (const line of text.split('\n')) {
    if (/^secrets:\s*(#.*)?$/.test(line)) inBlock = true
    else if (inBlock) {
      const m = line.match(/^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s*(#.*)?$/)
      if (m) out.push(m[1])
      else if (/^\S/.test(line)) inBlock = false
    }
  }
  return out
}
watch(spec, (text) => {
  for (const n of secretNames(text)) {
    if (!secrets.value.some((s) => s.key === n)) secrets.value.push({ key: n, value: '' })
  }
})

async function apply() {
  error.value = ''
  busy.value = true
  try {
    const map: Record<string, string> = {}
    for (const s of secrets.value) if (s.key.trim()) map[s.key.trim()] = s.value
    const app = await applySpec(spec.value, map)
    toast.success(`${app.name} ${app.created ? 'created. Deploying, usually about a minute' : 'updated. Redeploying'}.`)
    await router.push(`/apps/${app.id}`)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't apply the spec."
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <form class="card card-pad space-y-3" @submit.prevent="apply">
    <div>
      <div class="flex flex-wrap items-center justify-between gap-2">
        <label for="spec-text" class="label !mb-0">
          YAML spec
          <RouterLink to="/guide/user#4-yaml-spec-reference" target="_blank" class="link ml-2 text-xs font-normal">Spec reference</RouterLink>
        </label>
        <button type="button" class="btn-secondary btn-sm" @click="spec = EXAMPLE">Load example</button>
      </div>
      <p class="help">The same thing as the form, as a file. Applying an existing name updates that app.</p>
      <textarea
        id="spec-text"
        v-model="spec"
        rows="14"
        spellcheck="false"
        wrap="off"
        placeholder="Paste a spec, or press Load example."
        :aria-invalid="!!error || undefined"
        class="input mono mt-2"
      />
    </div>

    <div v-if="secrets.length">
      <p class="label">Secret values</p>
      <p class="help !mt-0 mb-2">Names come from <code class="mono">secrets:</code> in the spec. Values are encrypted and can't be shown again.</p>
      <div v-for="(s, i) in secrets" :key="i" class="mb-2 flex flex-col gap-2 sm:flex-row">
        <input v-model="s.key" placeholder="KEY" autocomplete="off" aria-label="Secret name" class="input mono sm:w-56" />
        <input v-model="s.value" type="password" placeholder="value" autocomplete="new-password" aria-label="Secret value" class="input mono sm:flex-1" />
        <IconButton :icon="Trash2" label="Remove secret" danger class="self-start sm:self-center" @click="secrets.splice(i, 1)" />
      </div>
    </div>
    <button type="button" class="btn-secondary btn-sm" @click="secrets.push({ key: '', value: '' })">+ Add secret</button>

    <pre v-if="error" role="alert" class="alert-error whitespace-pre-wrap font-sans">{{ error }}</pre>

    <div class="flex justify-end">
      <button type="submit" class="btn-primary" :disabled="busy || !spec.trim()">{{ busy ? 'Applying…' : 'Apply spec' }}</button>
    </div>
  </form>
</template>
