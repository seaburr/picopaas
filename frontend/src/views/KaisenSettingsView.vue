<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RotateCw } from 'lucide-vue-next'
import IconButton from '../components/IconButton.vue'
import KaisenKeyHelp from '../components/KaisenKeyHelp.vue'
import NavBar from '../components/NavBar.vue'
import PasswordField from '../components/PasswordField.vue'
import PreviewBadge from '../components/PreviewBadge.vue'
import { ApiError } from '../api/client'
import { getKaisenSettings, getKaisenSpend, saveKaisenSettings, testKaisen } from '../api/kaisen'
import { fmtUsd } from '../composables/kaisen'
import { useKaisenStore } from '../stores/kaisen'
import { useProjectsStore } from '../stores/projects'
import { useToastStore } from '../stores/toast'
import type { KaisenSettings, KaisenSettingsPayload } from '../types'

const projects = useProjectsStore()
const store = useKaisenStore()
const toast = useToastStore()

const s = ref<KaisenSettings | null>(null)
const monthUsd = ref<number | null>(null)
const loading = ref(true)
const error = ref('')
const saveError = ref('')
const saving = ref(false)
const fieldErr = reactive<Record<string, string>>({})
const testing = ref<'' | 'anthropic'>('')
const testResult = ref<{ ok: boolean; message: string } | null>(null)

const form = reactive({
  anthropic_key: '',
  clear_anthropic_key: false,
  model: '',
  effort: '',
  cap_run: 0,
  cap_day: 0,
  cap_build: 0,
  github_token: '',
  clear_github_token: false,
})

const pid = computed(() => projects.current?.id ?? null)
const readOnly = computed(() => !!s.value && !s.value.can_edit)

function fill(v: KaisenSettings) {
  s.value = v
  Object.assign(form, {
    anthropic_key: '', clear_anthropic_key: false, github_token: '', clear_github_token: false,
    model: v.model, effort: v.effort, cap_run: v.cap_run, cap_day: v.cap_day, cap_build: v.cap_build,
  })
}
const snapshot = () => JSON.stringify(form)
const clean = ref('')
const dirty = computed(() => snapshot() !== clean.value)

async function load() {
  loading.value = true
  error.value = ''
  testResult.value = null
  try {
    if (!projects.loaded) await projects.load()
    if (!pid.value) throw new ApiError('No project selected.', 0)
    const v = await getKaisenSettings(pid.value)
    fill(v)
    store.settings = v
    clean.value = snapshot()
    try {
      monthUsd.value = (await getKaisenSpend(pid.value)).month_usd
    } catch {
      monthUsd.value = null
    }
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load Kaisen settings."
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch(() => projects.currentId, (n, o) => { if (o !== n && projects.loaded) load() })

async function save() {
  if (!pid.value || readOnly.value) return
  saveError.value = ''
  for (const k of Object.keys(fieldErr)) delete fieldErr[k]
  const payload: KaisenSettingsPayload = {
    model: form.model, effort: form.effort,
    cap_run: Number(form.cap_run) || 0, cap_day: Number(form.cap_day) || 0, cap_build: Number(form.cap_build) || 0,
  }
  if (form.anthropic_key) payload.anthropic_key = form.anthropic_key
  if (form.clear_anthropic_key) payload.clear_anthropic_key = true
  if (form.github_token) payload.github_token = form.github_token
  if (form.clear_github_token) payload.clear_github_token = true
  saving.value = true
  try {
    const v = await saveKaisenSettings(pid.value, payload)
    fill(v)
    store.settings = v
    clean.value = snapshot()
    testResult.value = null
    toast.success('Kaisen settings saved.')
  } catch (e) {
    if (e instanceof ApiError) {
      Object.assign(fieldErr, e.fieldErrors)
      saveError.value = e.message
    } else saveError.value = "Couldn't save settings."
  } finally {
    saving.value = false
  }
}

async function test(which: 'anthropic') {
  if (!pid.value) return
  testing.value = which
  testResult.value = null
  try {
    testResult.value = await testKaisen(pid.value, which)
    if (testResult.value.ok) toast.success(testResult.value.message)
  } catch (e) {
    testResult.value = { ok: false, message: e instanceof ApiError ? e.message : 'The test could not run.' }
  } finally {
    testing.value = ''
  }
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main id="main" tabindex="-1" class="mx-auto max-w-6xl px-4 py-4 sm:px-6">
      <nav class="meta mb-2" aria-label="Breadcrumb"><RouterLink to="/kaisen" class="link">Kaisen</RouterLink><span aria-hidden="true"> / </span>Settings</nav>
      <div class="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 class="page-title">Kaisen settings</h1><PreviewBadge />
        <span v-if="projects.current" class="meta">Project: {{ projects.current.is_personal ? 'Personal' : projects.current.name }}</span>
      </div>

      <div v-if="error" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
        <span>{{ error }}</span>
        <IconButton :icon="RotateCw" label="Retry" @click="load" />
      </div>

      <div v-else-if="loading || !s" class="grid gap-3 lg:grid-cols-2" aria-busy="true" aria-label="Loading settings">
        <div class="card card-pad h-32 animate-pulse" />
        <div class="card card-pad h-24 animate-pulse" />
      </div>

      <form v-else class="grid gap-3 lg:grid-cols-2 lg:items-start" novalidate @submit.prevent="save">
        <p class="help lg:col-span-2">These settings belong to the project "{{ projects.current?.is_personal ? 'Personal' : projects.current?.name }}". Everyone who can run builds here spends from this key.</p>
        <p v-if="readOnly" class="alert-info lg:col-span-2" role="status">Only project owners can change these settings. You can see what is set.</p>

        <section class="card card-pad space-y-3" aria-labelledby="ks-anthropic">
          <h2 id="ks-anthropic" class="section-title">Anthropic</h2>
          <template v-if="readOnly">
            <p class="text-sm text-zinc-200">API key: {{ s.anthropic_key.set ? 'Set' : 'Not set' }}</p>
          </template>
          <template v-else>
            <PasswordField v-model="form.anthropic_key" label="API key" autocomplete="off" :required="false" :invalid="!!fieldErr.anthropic_key" :help="s.anthropic_key.set ? `Saved, ends in ...${s.anthropic_key.last4 ?? ''}. Leave blank to keep the saved key.` : 'Not set'" />
            <p v-if="fieldErr.anthropic_key" class="field-error" role="alert">{{ fieldErr.anthropic_key }}</p>
            <label v-if="s.anthropic_key.set" class="flex items-center gap-2 text-sm text-zinc-200"><input v-model="form.clear_anthropic_key" type="checkbox" class="check" /> Clear stored key</label>
          </template>
          <div class="grid gap-3 sm:grid-cols-2">
            <div>
              <label for="ks-model" class="label">Model</label>
              <select id="ks-model" v-model="form.model" class="input" :disabled="readOnly"><option v-for="m in s.models" :key="m" :value="m">{{ m }}</option></select>
            </div>
            <div>
              <label for="ks-effort" class="label">Effort</label>
              <select id="ks-effort" v-model="form.effort" class="input" :disabled="readOnly"><option v-for="m in s.efforts" :key="m" :value="m">{{ m }}</option></select>
            </div>
          </div>
          <KaisenKeyHelp kind="anthropic" />
        </section>

        <section class="card card-pad" aria-labelledby="ks-caps">
          <h2 id="ks-caps" class="section-title mb-2">Spend limits (USD)</h2>
          <div class="grid gap-3 sm:grid-cols-3">
            <div><label for="ks-cap-run" class="label">Per run</label><input id="ks-cap-run" v-model.number="form.cap_run" type="number" step="0.01" min="0" class="input" :disabled="readOnly" /></div>
            <div><label for="ks-cap-day" class="label">Per day</label><input id="ks-cap-day" v-model.number="form.cap_day" type="number" step="0.01" min="0" class="input" :disabled="readOnly" /></div>
            <div><label for="ks-cap-build" class="label">Per build</label><input id="ks-cap-build" v-model.number="form.cap_build" type="number" step="0.01" min="0" class="input" :disabled="readOnly" /></div>
          </div>
          <p v-if="monthUsd !== null" class="meta mt-2">This month: <span class="mono">{{ fmtUsd(monthUsd) }}</span></p>
        </section>

        <section class="card card-pad space-y-3" aria-labelledby="ks-output">
          <h2 id="ks-output" class="section-title">GitHub</h2>
          <p class="help">A GitHub token every build in this project can use. Each build picks its own repository; it can use this token or its own. Anyone who can run builds here can push with it to any repository it can write, so use a fine-grained token limited to the repositories you mean.</p>
          <p v-if="readOnly" class="text-sm text-zinc-200">Access token: {{ s.github_token.set ? 'Set' : 'Not set' }}</p>
          <template v-else>
            <PasswordField v-model="form.github_token" label="Access token" autocomplete="off" :required="false" :invalid="!!fieldErr.github_token" :help="s.github_token.set ? `Saved, ends in ...${s.github_token.last4 ?? ''}. Leave blank to keep the saved token.` : 'Not set'" />
            <p v-if="fieldErr.github_token" class="field-error" role="alert">{{ fieldErr.github_token }}</p>
            <label v-if="s.github_token.set" class="flex items-center gap-2 text-sm text-zinc-200"><input v-model="form.clear_github_token" type="checkbox" class="check" /> Clear stored token</label>
            <KaisenKeyHelp kind="github" :open="!s.github_token.set" />
            <p class="help">To check the token, open a build and press Test on its Output card.</p>
          </template>
        </section>

        <div v-if="saveError" role="alert" class="alert-error whitespace-pre-wrap lg:col-span-2">{{ saveError }}</div>
        <div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between lg:col-span-2">
          <button v-if="!readOnly" type="submit" class="btn-primary" :disabled="saving">{{ saving ? 'Saving…' : 'Save settings' }}</button>
          <div class="flex flex-col gap-2 sm:flex-row">
            <button type="button" class="btn-secondary" :disabled="testing !== '' || dirty" @click="test('anthropic')">{{ testing === 'anthropic' ? 'Testing…' : 'Test Anthropic' }}</button>
          </div>
        </div>
        <p v-if="dirty && !readOnly" class="help lg:col-span-2">Save first, then test.</p>
        <div role="status" class="lg:col-span-2">
          <p v-if="testResult" :class="testResult.ok ? 'alert-success' : 'alert-error'">{{ testResult.message }}</p>
        </div>
      </form>
    </main>
  </div>
</template>
