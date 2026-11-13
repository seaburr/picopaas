<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useProjectTitle } from '../composables/projectTitle'
import { useRouter } from 'vue-router'
import BuildOutputFields from '../components/BuildOutputFields.vue'
import PreviewBadge from '../components/PreviewBadge.vue'
import { ApiError } from '../api/client'
import { createBuild, getKaisenSettings, improveBrief, runBuild } from '../api/kaisen'
import { listTemplates } from '../api/templates'
import AttachComposer from '../components/AttachComposer.vue'
import { outputPayload, outputFieldErrors, blankOutputForm } from '../composables/kaisen'
import { STARTERS, needsPg, PG_UNAVAILABLE_MSG, type Starter } from '../composables/kaisenStarters'
import { checkBrief } from '../composables/briefCheck'
import { useToastStore } from '../stores/toast'
import SuspendedBanner from '../components/SuspendedBanner.vue'
import { SUSPENDED_TITLE, useProjectSuspension } from '../composables/projectSuspension'
import { useKaisenStore } from '../stores/kaisen'
import { useProjectsStore } from '../stores/projects'
import { projectKaisen, projectLabel } from '../utils/paths'
import type { KaisenUploadsAvailability } from '../types'

const props = defineProps<{ pid: string }>()
const router = useRouter()
const projects = useProjectsStore()
const store = useKaisenStore()
const toast = useToastStore()

const maxBrief = ref(8000) // replaced by the server value (config.KAISEN_BRIEF_MAX) once settings load
const name = ref('')
const brief = ref('')
const undo = ref<{ name: string; brief: string } | null>(null)
// What the last one-click fill was, so the result is announced even when it lands out of view above the table.
const filled = ref<{ kind: 'starter' | 'suggestion'; label: string } | null>(null)
const warnings = computed(() => checkBrief(brief.value))
// PostgreSQL availability comes from GET /templates (`available` is false on PostgreSQL templates when no server exists).
const pgOk = ref(true)
async function loadPg() {
  try {
    const t = await listTemplates()
    pgOk.value = !t.some((x) => x.wants_db && x.db_engine === 'pg' && x.available === false)
  } catch {
    pgOk.value = true // unknown: the server still refuses the database later
  }
}
const starterOff = (st: Starter) => needsPg(st) && !pgOk.value
// Improve brief: only on a click, one server call, and nothing is replaced until "Use this".
const improving = ref(false)
const suggestion = ref<{ brief: string; usd: number } | null>(null)
const improveError = ref('')
// Every successful press cost money, used or not: all of them go to the build on create so its cost includes them.
const MAX_IMPROVE_IDS = 20
const improveIds = ref<number[]>([])
const improveFull = computed(() => improveIds.value.length >= MAX_IMPROVE_IDS)
const improveField = () => (improveIds.value.length ? { improve_ids: improveIds.value } : {})
async function improve() {
  improveError.value = ''
  suggestion.value = null
  if (!brief.value.trim() || noKey.value || overLimit.value || improveFull.value) return
  improving.value = true
  try {
    const r = await improveBrief({ project_id: projectId.value, brief: brief.value.trim() })
    suggestion.value = { brief: r.brief, usd: r.usd }
    if (typeof r.improve_id === 'number') improveIds.value = [...improveIds.value, r.improve_id]
  } catch (e) {
    improveError.value = e instanceof ApiError ? e.message : 'Could not improve the brief. Try again.'
  } finally {
    improving.value = false
  }
}
// A suggestion is a rewrite of the brief as it was when Improve brief was pressed; drop it once the brief is edited.
watch(brief, () => { if (!improving.value) suggestion.value = null })
function acceptSuggestion() {
  if (!suggestion.value) return
  undo.value = { name: name.value, brief: brief.value }
  filled.value = { kind: 'suggestion', label: '' }
  brief.value = suggestion.value.brief
  suggestion.value = null
}
function useStarter(st: Starter) {
  if (starterOff(st)) return
  suggestion.value = null
  // Replace, and keep the previous text one click away.
  undo.value = name.value.trim() || brief.value.trim() ? { name: name.value, brief: brief.value } : null
  filled.value = { kind: 'starter', label: st.title }
  name.value = st.name
  brief.value = st.brief
}
function undoStarter() {
  if (!undo.value) return
  name.value = undo.value.name
  brief.value = undo.value.brief
  undo.value = null
  filled.value = null
}
const projectId = computed(() => Number(props.pid))
const projName = computed(() => projectLabel(curProject.value))
const { project: curProject, suspended: projSuspended } = useProjectSuspension(projectId)
useProjectTitle('New build', projName) // after curProject: its immediate watch reads projName (TDZ crash otherwise)
const busy = ref(false)
const error = ref('')
const nameError = ref('')
const briefError = ref('')
const noKey = ref(false)
const outForm = reactive(blankOutputForm())
const outErr = reactive<Record<string, string>>({})
const outOpen = ref(false)
const composer = ref<InstanceType<typeof AttachComposer> | null>(null)
// Upload availability for the current project comes with its Kaisen settings.
const uploadsInfo = computed<KaisenUploadsAvailability | undefined>(() => {
  const s = store.settings
  if (!s) return undefined
  return {
    uploads_enabled: s.uploads_enabled === true, uploads_project_enabled: s.uploads_enabled === true, uploads_platform_enabled: true,
    uploads_disabled_reason: s.uploads_disabled_reason ?? null, uploads_not_enabled_label: s.uploads_not_enabled_label ?? 'Not enabled for this project yet', uploads_notice: s.uploads_notice ?? '',
  }
})
// Draft flow: the build exists (idle) once files are involved; `stage` drives the progress panel.
const draftId = ref<number | null>(null)
const stage = ref<'' | 'creating' | 'uploading' | 'partial' | 'starting'>('')
const partialMsg = ref('')
const chipList = computed(() => composer.value?.chips ?? [])
const uploadedCount = computed(() => chipList.value.filter((c) => c.status === 'ready').length)
const totalCount = computed(() => chipList.value.filter((c) => c.file || c.status === 'ready').length)

onMounted(async () => {
  if (!projects.loaded) await projects.load()
  void loadPg()
  await checkKey()
})
watch(() => props.pid, () => void checkKey())
async function checkKey() {
  if (!projectId.value) return
  try {
    const s = await getKaisenSettings(projectId.value)
    store.settings = s
    if (s.brief_max) maxBrief.value = s.brief_max
    noKey.value = !s.anthropic_key.set
    if (!s.github_token.set && outForm.github_auth === 'project') outForm.github_auth = 'build'
  } catch {
    noKey.value = false // unknown: let the server decide
  }
}

const overLimit = computed(() => brief.value.length > maxBrief.value)
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
    const heldN = composer.value?.held.length ?? 0
    if (heldN === 0 && draftId.value === null) {
      const b = await createBuild({ project_id: projectId.value, name: n, brief: brief.value.trim(), output: outputPayload(outForm), ...improveField() })
      store.upsert(b)
      if (b.start_error) toast.error(`Build created but not started: ${b.start_error}`)
      router.push(`/kaisen/${b.id}`)
      return
    }
    if (draftId.value === null) {
      stage.value = 'creating'
      const b = await createBuild({ project_id: projectId.value, name: n, brief: brief.value.trim(), output: outputPayload(outForm), start: false, ...improveField() })
      store.upsert(b)
      draftId.value = b.id
    }
    await uploadThenStart()
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

async function uploadThenStart() {
  const id = draftId.value
  if (id === null || !composer.value) return
  stage.value = 'uploading'
  const failed = await composer.value.uploadHeld(id)
  if (failed.length) {
    stage.value = 'partial'
    partialMsg.value = `${failed.length} ${failed.length === 1 ? 'file' : 'files'} didn't upload. Your build is saved as a draft.`
    return
  }
  await startDraft()
}
async function startDraft() {
  const id = draftId.value
  if (id === null) return
  stage.value = 'starting'
  try {
    const b = await runBuild(id)
    store.upsert(b)
  } catch (e) {
    toast.error(`Build saved but not started: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  }
  router.push(`/kaisen/${id}`)
}
async function retryFailed() {
  busy.value = true
  try {
    await uploadThenStart()
  } finally {
    busy.value = false
  }
}
async function startWithout() {
  busy.value = true
  try {
    await startDraft()
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div>
    <main id="main" tabindex="-1" class="page-main">
      <SuspendedBanner :project="curProject" kaisen />
      <nav class="meta mb-2" aria-label="Breadcrumb">
        <RouterLink :to="`/projects/${pid}`" class="link">{{ projName || 'Project' }}</RouterLink><span aria-hidden="true"> / </span>
        <RouterLink :to="projectKaisen(projectId)" class="link">Kaisen</RouterLink><span aria-hidden="true"> / </span>New build
      </nav>
      <div class="mb-3">
        <div class="flex flex-wrap items-center gap-2"><h1 class="page-title">New build<template v-if="projName"> in {{ projName }}</template></h1><PreviewBadge /></div>
        <p class="help">Describe the app. Kaisen builds it, deploys it as an app in {{ projName || 'this project' }} and asks if it needs to know something. Runs are billed to this project's Anthropic key.</p>
      </div>

      <p v-if="noKey" class="alert-warn mb-3" role="status">
        <template v-if="canEditKey">Kaisen needs an Anthropic API key added to this project before it can build. <RouterLink :to="projectKaisen(projectId, '/settings')" class="link">Open Kaisen settings</RouterLink></template>
        <template v-else>Kaisen isn't set up in {{ projName }} yet. Ask an owner to add an Anthropic API key.</template>
      </p>

      <form class="sec space-y-4 pt-4 [&_.input]:max-w-3xl" novalidate @submit.prevent="submit">
        <div>
          <label for="kb-name" class="label">Name</label>
          <input id="kb-name" v-model="name" class="input" autocomplete="off" maxlength="60" :aria-invalid="nameError ? 'true' : undefined" aria-describedby="kb-name-help" placeholder="Todo list" />
          <p v-if="nameError" class="field-error" role="alert">{{ nameError }}</p>
          <p id="kb-name-help" class="help">Shown in Kaisen and as the app's name. Like any app, its address is generated for you.</p>
        </div>
        <div>
          <label for="kb-brief" class="label">What should it do?</label>
          <AttachComposer
            ref="composer" v-model="brief" :build-id="null" :summary="uploadsInfo" label="What should it do?" input-id="kb-brief" describedby="kb-brief-help" :invalid="!!briefError || overLimit"
            :placeholder="uploadsInfo && !uploadsInfo.uploads_enabled ? 'Describe the app.' : 'Describe the app. Drop, paste or attach files.'" no-submit :locked="projSuspended ? SUSPENDED_TITLE : ''" :can-attach="stage === '' || stage === 'partial'" :rows="8"
          />
          <p v-if="briefError" class="field-error" role="alert">{{ briefError }}</p>
          <p id="kb-brief-help" class="help" :aria-live="overLimit ? 'polite' : undefined">{{ brief.length }} / {{ maxBrief }}<template v-if="overLimit"> · Too long. Shorten it.</template></p>
          <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
            <button type="button" class="link text-sm" data-testid="improve" :disabled="improving || noKey || !brief.trim() || overLimit || improveFull" :title="noKey ? 'Add an Anthropic API key in Kaisen settings first.' : improveFull ? `Limit of ${MAX_IMPROVE_IDS} improvements per build reached.` : undefined" @click="improve">{{ improving ? 'Improving…' : 'Improve brief' }}</button>
            <span v-if="noKey" class="help mt-0">Needs an Anthropic API key. A project owner can add one in Kaisen settings.</span>
            <span v-else class="help mt-0">Each "Improve brief" press is billed to this project's Anthropic key and counts toward the project's Kaisen daily cap. Its cost is added to this build's cost when you press "Start building". Your brief changes only if you press "Use this".</span>
            <span class="sr-only" role="status">{{ improving ? 'Improving the brief' : suggestion ? 'Suggested brief ready' : '' }}</span>
            <span v-if="filled?.kind === 'suggestion' && undo" class="help mt-0" data-testid="suggestion-note">Brief replaced by the suggestion. <button type="button" class="link" @click="undoStarter">Undo</button></span>
          </div>
          <p v-if="improveError" class="field-error" role="alert" data-testid="improve-error">{{ improveError }}</p>
          <div v-if="suggestion" class="mt-2 rounded border border-zinc-700 p-3" data-testid="suggestion" role="region" aria-label="Suggested brief">
            <p class="label">Suggested brief</p>
            <pre class="mono mt-1 max-h-72 overflow-auto text-sm whitespace-pre-wrap wrap-break-word text-zinc-200" data-testid="suggestion-text">{{ suggestion.brief }}</pre>
            <p class="help mt-1">Check it still says what you want. Cost about ${{ suggestion.usd.toFixed(4) }}.</p>
            <div class="mt-2 flex flex-wrap gap-2">
              <button type="button" class="btn-primary btn-sm" data-testid="suggestion-use" @click="acceptSuggestion">Use this</button>
              <button type="button" class="btn-secondary btn-sm" data-testid="suggestion-keep" @click="suggestion = null">Keep mine</button>
            </div>
          </div>
          <ul v-if="warnings.length" class="mt-2 space-y-1" data-testid="brief-warnings" aria-label="Brief check">
            <li v-for="w in warnings" :key="w.id" class="alert-warn" :data-warning="w.id">{{ w.text }}</li>
            <li class="help">These are hints from a keyword check. They do not stop you from starting.</li>
          </ul>
        </div>
        <section aria-labelledby="kb-starters" data-testid="starters">
          <div class="flex flex-wrap items-baseline gap-x-3">
            <h2 id="kb-starters" class="label">Starters</h2>
            <span v-if="filled?.kind === 'starter'" class="text-xs text-zinc-300" role="status" data-testid="starter-note">Name and brief filled from {{ filled.label }}.<template v-if="undo">{{ ' ' }}<button type="button" class="link" @click="undoStarter">Undo</button></template></span>
          </div>
          <p class="help mb-2">Fills the name and brief above and replaces what is there. Nothing starts until you press Start building. Cost is one recorded run on Haiku 5.5; yours will differ.</p>
          <table class="tbl tbl-stack">
            <colgroup><col class="w-[18%]"><col><col class="w-[16%]"><col class="w-[9%]"><col class="w-36"></colgroup>
            <thead><tr><th>Starter</th><th>What it builds</th><th>Storage</th><th>Cost</th><th class="text-right"><span class="sr-only">Actions</span></th></tr></thead>
            <tbody>
              <tr v-for="st in STARTERS" :key="st.title" class="align-top" :data-starter="st.title">
                <td class="font-medium text-white wrap-break-word">{{ st.title }}</td>
                <td class="wrap-break-word text-zinc-300">{{ st.description }}<span v-if="starterOff(st)" class="mt-1 block text-amber-300" data-testid="pg-off">{{ PG_UNAVAILABLE_MSG }}</span></td>
                <td data-label="Storage"><span class="flex flex-wrap gap-1"><span v-for="p in st.storage" :key="p" class="rounded border border-zinc-700 px-1.5 text-xs text-zinc-300">{{ p }}</span></span></td>
                <td class="mono text-zinc-300" :class="!st.cost && 'max-md:hidden'" data-label="Cost"><span v-if="st.cost" :title="st.costSource">~{{ st.cost }}</span><template v-else><span aria-hidden="true" class="text-zinc-500">-</span><span class="sr-only">not recorded</span></template></td>
                <td><div class="row-acts"><button type="button" class="row-act" :class="!starterOff(st) && 'text-white'" :disabled="starterOff(st)" :data-use-starter="st.title" @click="useStarter(st)">Use this brief<span class="sr-only"> ({{ st.title }})</span></button></div></td>
              </tr>
            </tbody>
          </table>
        </section>

        <details class="disc help" :open="outOpen" @toggle="outOpen = ($event.target as HTMLDetailsElement).open">
          <summary>Output (optional)</summary>
          <div class="mt-2">
            <p class="help mb-2">You can always download the code. Pick a GitHub repository for this app if you want it pushed too. You can change this later on the build.</p>
            <BuildOutputFields v-model="outForm" id-prefix="kb-out" :project-token-set="!!store.settings?.github_token.set" :project-id="projectId" :errors="outErr" />
          </div>
        </details>

        <div v-if="stage && stage !== 'partial'" class="alert-info" role="status" aria-live="polite">
          <p v-if="stage === 'creating'">Creating the build…</p>
          <p v-else-if="stage === 'uploading'">Created build. Uploading {{ uploadedCount + 1 > totalCount ? totalCount : uploadedCount + 1 }} of {{ totalCount }}…</p>
          <p v-else>Starting the build…</p>
        </div>
        <div v-if="stage === 'partial'" class="alert-warn space-y-2" role="alert">
          <p>{{ partialMsg }}</p>
          <p class="flex flex-wrap gap-2">
            <button type="button" class="btn-secondary btn-sm" :disabled="busy" @click="retryFailed">Retry failed {{ composer && composer.failed.length > 1 ? 'files' : 'file' }}</button>
            <button type="button" class="btn-primary btn-sm" :disabled="busy" @click="startWithout">Start building without {{ composer && composer.failed.length > 1 ? 'them' : 'it' }}</button>
            <RouterLink v-if="draftId" :to="`/kaisen/${draftId}`" class="btn-secondary btn-sm">Open the draft</RouterLink>
          </p>
        </div>
        <div v-if="error" role="alert" class="alert-error whitespace-pre-wrap">{{ error }}</div>
        <div class="flex flex-col gap-2 sm:flex-row">
          <button type="submit" class="btn-primary" :disabled="busy || noKey || overLimit || projSuspended || stage === 'partial' || (composer?.uploading ?? 0) > 0" :title="projSuspended ? SUSPENDED_TITLE : undefined">{{ busy ? 'Starting…' : 'Start building' }}</button>
          <RouterLink :to="projectKaisen(projectId)" class="btn-secondary">Cancel</RouterLink>
        </div>
      </form>
    </main>
  </div>
</template>
