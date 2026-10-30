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

const maxBrief = ref(8000) // replaced by the server value (config.KAISEN_BRIEF_MAX) once settings load
const STARTERS = [
  { title: "Fancy Docs", name: "Fancy Docs", brief: "I want a Markdown note-taking application that saves to a MySQL database. The editor is on the left and the rendered Markdown is on the right, with a nav tree in the center. Pages can be nested underneath other pages, and the nav tree can be used to move pages around in the tree (drag and drop, plus buttons for keyboard users). The editor pane can be collapsed. Support image uploads that are stored in the MySQL database and can be referenced in the Markdown. Include a dark mode toggle for the entire page, remembered in the browser." },
  { title: "Team standup board", name: "Team standup board", brief: "Build a small standup board with Python and MySQL. Anyone can open the page, type their name and post three short fields for a chosen date: yesterday, today and blockers. There is no login. The main screen is a day view: a date picker with previous and next day buttons, and one card per person who posted that day, with blockers highlighted. Posting again for the same person and date replaces the earlier post. A second page shows one person's history, newest first, reachable by clicking a name. Save everything in MySQL. Include a dark mode toggle remembered in the browser. Out of scope: accounts, passwords, email or chat notifications, editing other people's posts." },
  { title: "Link shortener with click counts", name: "Link shortener", brief: "Build a link shortener with Python and MySQL. The home page has a form to paste a long URL and optionally choose a short code; if no code is given, generate a random 6-character one. Only accept http and https URLs, and show a clear error for anything else or for a code that is already taken. Visiting /<code> redirects to the long URL and adds one to that link's click count. A stats page lists every link with its code, target, created date and click count, sorted by clicks, with a copy button for each short URL. Save everything in MySQL. Out of scope: accounts, QR codes, expiry, analytics beyond the count." },
  { title: "Habit tracker with streaks", name: "Habit tracker", brief: "Build a habit tracker as a static page that saves to the browser's localStorage, with no server. I can add and remove habits. Each habit shows a month grid where I click a day to tick it on or off, with buttons to move to the previous and next month. Next to each habit show the current streak (consecutive ticked days ending today or yesterday) and the best streak ever. Add Export and Import buttons for all data as a JSON file, and validate the file on import. Support a dark mode toggle remembered in the browser. Out of scope: reminders, accounts, syncing between devices." },
  { title: "Recipe scaler", name: "Recipe scaler", brief: "Build a recipe scaler as a static page with no server and nothing saved. I paste a recipe's ingredient list, one ingredient per line (like \"1 1/2 cups flour\"), and set the original and wanted number of servings. The page shows the scaled list, handling fractions and decimals and showing results as friendly fractions. A toggle converts between metric and US units (cups, tablespoons, teaspoons, ounces, pounds, grams, millilitres) and leaves lines it cannot parse unchanged. Include a print view that shows only the scaled recipe in a clean layout. Light theme only. Out of scope: saving recipes, importing from web pages, nutrition information." },
]
const name = ref('')
const brief = ref('')
const undo = ref<{ name: string; brief: string } | null>(null)
function useStarter(st: { name: string; brief: string }) {
  // Replace, and keep the previous text one click away.
  undo.value = name.value.trim() || brief.value.trim() ? { name: name.value, brief: brief.value } : null
  name.value = st.name
  brief.value = st.brief
}
function undoStarter() {
  if (!undo.value) return
  name.value = undo.value.name
  brief.value = undo.value.brief
  undo.value = null
}
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
    <main id="main" tabindex="-1" class="page-main">
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

      <form class="card card-pad space-y-4 [&_.input]:max-w-3xl" novalidate @submit.prevent="submit">
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
          <p id="kb-brief-help" class="help" :aria-live="overLimit ? 'polite' : undefined">{{ brief.length }} / {{ maxBrief }}<template v-if="overLimit"> · Too long. Shorten it.</template></p>
          <div class="mt-2 flex flex-wrap items-center gap-1">
            <span class="help mr-1">Start from:</span>
            <button v-for="st in STARTERS" :key="st.title" type="button" class="btn-secondary btn-sm" :title="st.brief" @click="useStarter(st)">{{ st.title }}</button>
            <button v-if="undo" type="button" class="link text-sm" @click="undoStarter">Undo</button>
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
