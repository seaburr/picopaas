<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import BuildOutputFields from '../components/BuildOutputFields.vue'
import NavBar from '../components/NavBar.vue'
import PreviewBadge from '../components/PreviewBadge.vue'
import { ApiError } from '../api/client'
import { createBuild, getKaisenSettings } from '../api/kaisen'
import { outputPayload, outputFieldErrors, blankOutputForm } from '../composables/kaisen'
import { useToastStore } from '../stores/toast'
import { useKaisenStore } from '../stores/kaisen'
import { useProjectsStore } from '../stores/projects'

const router = useRouter()
const projects = useProjectsStore()
const store = useKaisenStore()
const toast = useToastStore()

const MAX_BRIEF = 4000
const EXAMPLES = ['A URL shortener with click counts', 'A markdown notes page that saves to a database']
const name = ref('')
const brief = ref('')
const projectId = ref<number | null>(null)
const busy = ref(false)
const error = ref('')
const nameError = ref('')
const briefError = ref('')
const noKey = ref(false)
const outForm = reactive(blankOutputForm())
const outErr = reactive<Record<string, string>>({})
const outOpen = ref(false)

onMounted(async () => {
  if (!projects.loaded) await projects.load()
  projectId.value = projects.current?.id ?? null
  await checkKey()
})
async function checkKey() {
  if (!projectId.value) return
  try {
    const s = await getKaisenSettings(projectId.value)
    store.settings = s
    noKey.value = !s.anthropic_key.set
    if (!s.github_token.set && outForm.github_auth === 'project') outForm.github_auth = 'build'
  } catch {
    noKey.value = false // unknown: let the server decide
  }
}

const overLimit = computed(() => brief.value.length > MAX_BRIEF)
const canEditKey = computed(() => !!store.settings?.can_edit)

async function submit() {
  error.value = nameError.value = briefError.value = ''
  const n = name.value.trim()
  if (!n) {
    nameError.value = 'Give the build a name.'
    return
  }
  if (!brief.value.trim()) {
    briefError.value = 'Describe what the app should do.'
    return
  }
  if (overLimit.value) return
  for (const k of Object.keys(outErr)) delete outErr[k]
  if (outForm.output_mode === 'github' && !outForm.github_repo.trim()) {
    outErr.github_repo = 'Enter a GitHub repository URL.'
    outOpen.value = true
    return
  }
  busy.value = true
  try {
    const b = await createBuild({ project_id: projectId.value, name: n, brief: brief.value.trim(), output: outputPayload(outForm) })
    store.upsert(b)
    if (b.start_error) toast.error(`Build created but not started: ${b.start_error}`)
    router.push(`/kaisen/${b.id}`)
  } catch (e) {
    if (e instanceof ApiError) {
      nameError.value = e.fieldErrors.name ?? ''
      briefError.value = e.fieldErrors.brief ?? ''
      const oe = outputFieldErrors(e.fieldErrors)
      outErr.github_repo = oe.github_repo ?? ''
      outErr.github_token = oe.github_token ?? ''
      if (outErr.github_repo || outErr.github_token) outOpen.value = true
      error.value = nameError.value || briefError.value || outErr.github_repo || outErr.github_token ? '' : e.message
      if (e.status === 409 && !nameError.value) nameError.value = 'You already have an app with that name.'
    } else error.value = "Couldn't start the build."
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main id="main" tabindex="-1" class="mx-auto max-w-2xl px-4 py-4 sm:px-6">
      <nav class="meta mb-2" aria-label="Breadcrumb">
        <RouterLink to="/kaisen" class="link">Kaisen</RouterLink><span aria-hidden="true"> / </span>New build
      </nav>
      <div class="mb-3">
        <div class="flex flex-wrap items-center gap-2"><h1 class="page-title">New build</h1><PreviewBadge /></div>
        <p class="help">Describe the app. Kaisen builds it, deploys it as an app in the project you pick, and asks if it needs to know something.</p>
      </div>

      <p v-if="noKey" class="alert-warn mb-3" role="status">
        Kaisen needs an Anthropic API key for this project before it can build.
        <RouterLink v-if="canEditKey" to="/kaisen/settings" class="link">Add it in Kaisen settings</RouterLink>
        <template v-else>Ask a project owner to add one.</template>
      </p>

      <form class="card card-pad space-y-4" novalidate @submit.prevent="submit">
        <div>
          <label for="kb-name" class="label">Name</label>
          <input id="kb-name" v-model="name" class="input" autocomplete="off" maxlength="60" :aria-invalid="nameError ? 'true' : undefined" aria-describedby="kb-name-help" placeholder="Todo list" />
          <p v-if="nameError" class="field-error" role="alert">{{ nameError }}</p>
          <p id="kb-name-help" class="help">Shown in Kaisen and as the app's name. Like any app, its address is generated for you.</p>
        </div>
        <div>
          <label for="kb-brief" class="label">What should it do?</label>
          <textarea id="kb-brief" v-model="brief" class="input min-h-32" autocomplete="off" :aria-invalid="briefError || overLimit ? 'true' : undefined" aria-describedby="kb-brief-help" />
          <p v-if="briefError" class="field-error" role="alert">{{ briefError }}</p>
          <p id="kb-brief-help" class="help" :aria-live="overLimit ? 'polite' : undefined">{{ brief.length }} / {{ MAX_BRIEF }}<template v-if="overLimit"> · Too long. Shorten it.</template></p>
          <div class="mt-2 flex flex-col items-start gap-1 sm:flex-row sm:flex-wrap sm:items-center">
            <span class="help mr-1">Examples:</span>
            <button v-for="ex in EXAMPLES" :key="ex" type="button" class="btn-secondary btn-sm text-left" @click="brief = ex">{{ ex }}</button>
          </div>
        </div>
        <div v-if="projects.hasShared">
          <label for="kb-project" class="label">Project</label>
          <select id="kb-project" v-model="projectId" class="input" aria-describedby="kb-project-help" @change="checkKey">
            <option v-for="p in projects.projects" :key="p.id" :value="p.id">{{ p.is_personal ? 'Personal' : p.name }}</option>
          </select>
          <p id="kb-project-help" class="help">Kaisen deploys into this project and spends from its Anthropic key.</p>
        </div>
        <p v-else class="help">Deploys to your personal apps.</p>

        <details class="help" :open="outOpen" @toggle="outOpen = ($event.target as HTMLDetailsElement).open">
          <summary>Output (optional)</summary>
          <div class="mt-2">
            <p class="help mb-2">You can always download the code. Pick a GitHub repository for this app if you want it pushed too. You can change this later on the build.</p>
            <BuildOutputFields v-model="outForm" id-prefix="kb-out" :project-token-set="!!store.settings?.github_token.set" :errors="outErr" />
          </div>
        </details>

        <div v-if="error" role="alert" class="alert-error whitespace-pre-wrap">{{ error }}</div>
        <div class="flex flex-col gap-2 sm:flex-row">
          <button type="submit" class="btn-primary" :disabled="busy || noKey || overLimit">{{ busy ? 'Starting…' : 'Start building' }}</button>
          <RouterLink to="/kaisen" class="btn-secondary">Cancel</RouterLink>
        </div>
      </form>
    </main>
  </div>
</template>
