<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import AppNotFound from '../components/AppNotFound.vue'
import AppStatusNote from '../components/AppStatusNote.vue'
import BuildFiles from '../components/BuildFiles.vue'
import BuildFilesDrawer from '../components/BuildFilesDrawer.vue'
import BuildConversation from '../components/BuildConversation.vue'
import BuildOutputFields from '../components/BuildOutputFields.vue'
import BuildLastDeployment from '../components/BuildLastDeployment.vue'
import BuildPreviewPane from '../components/BuildPreviewPane.vue'
import BuildSecrets from '../components/BuildSecrets.vue'
import BuildRestorePoints from '../components/BuildRestorePoints.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DeleteAppDialog from '../components/DeleteAppDialog.vue'
import HostLink from '../components/HostLink.vue'
import IconButton from '../components/IconButton.vue'
import PreviewBadge from '../components/PreviewBadge.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError, downloadFile } from '../api/client'
import { getKaisenSettings, answerInbox, deployBuild, dismissInbox, getBuild, getPushJob, interruptRun, pushBuild, runBuild, saveBuildOutput, sendFeedback, testBuildOutput } from '../api/kaisen'
import { deleteApp, getApp } from '../api/apps'
import { relTime, useNow } from '../composables/appUi'
import { blankOutputForm, costHint, fmtUsd, isActive, outputFieldErrors, outputPayload, outputToForm } from '../composables/kaisen'
import { statusLook } from '../composables/statusTone'
import { useAuthStore } from '../stores/auth'
import { useKaisenStore } from '../stores/kaisen'
import { projectKaisen, projectLabel } from '../utils/paths'
import { useToastStore } from '../stores/toast'
import type { App, KaisenBuildDetail, KaisenPushJob, KaisenSettings } from '../types'
import SuspendedBanner from '../components/SuspendedBanner.vue'
import { SUSPENDED_TITLE, useProjectSuspension } from '../composables/projectSuspension'

const props = defineProps<{ id: string }>()
const router = useRouter()
const auth = useAuthStore()
const store = useKaisenStore()
const toast = useToastStore()
const now = useNow()

const build = ref<KaisenBuildDetail | null>(null)
const app = ref<App | null>(null)
const loadError = ref('')
const notFound = ref(false)
const announce = ref('')
const actionError = ref('')
const busy = ref('') // '' | 'feedback' | 'answer' | 'deploy' | 'push' | 'resume' | 'interrupt' | 'download'
const answerBusy = ref<number | null>(null)
const feedback = ref('')
const filesOpen = ref(false)
const filesFocus = ref<number | null>(null)
function openFiles(uid?: number) {
  filesFocus.value = uid ?? null
  filesOpen.value = true
}
// ---- layout: the chat fills the viewport below whatever sits above it; the rail is beside it (>= 1024px) or a disclosure ----
const mq = typeof matchMedia === 'function' ? matchMedia('(min-width: 1024px)') : null
const desktop = ref(mq?.matches ?? true)
const railOpen = ref(false)
function onRailToggle(e: Event) {
  if (!desktop.value) railOpen.value = (e.target as HTMLDetailsElement).open
}
const aboveEl = ref<HTMLElement | null>(null)
const chatCol = ref<HTMLElement | null>(null)
const chatH = ref(520)
function measure() {
  const el = chatCol.value
  if (!el || (!desktop.value && railOpen.value)) return
  const top = el.getBoundingClientRect().top + window.scrollY
  chatH.value = Math.max(380, Math.floor(window.innerHeight - top - 12))
}
function onMq(e: MediaQueryListEvent) {
  desktop.value = e.matches
  nextTick(measure)
}
let aboveRo: ResizeObserver | null = null
onMounted(() => {
  mq?.addEventListener('change', onMq)
  window.addEventListener('resize', measure)
})
watch(chatCol, (el) => {
  if (!el) return
  nextTick(measure)
  aboveRo?.disconnect()
  if (typeof ResizeObserver !== 'undefined' && aboveEl.value) {
    aboveRo = new ResizeObserver(measure)
    aboveRo.observe(aboveEl.value)
  }
})
onBeforeUnmount(() => {
  mq?.removeEventListener('change', onMq)
  window.removeEventListener('resize', measure)
  aboveRo?.disconnect()
})
const confirmDelete = ref(false)
const alsoApp = ref(false)
const deleting = ref(false)

const buildId = computed(() => Number(props.id))
const active = computed(() => !!build.value && isActive(build.value.status))
const { project: buildProject, suspended: projSuspended } = useProjectSuspension(() => build.value?.project_id)
const canRun = computed(() => !!build.value?.can_run && auth.user?.features?.kaisen !== false && !projSuspended.value)
const you = computed(() => build.value?.created_by.id === auth.user?.id)
const inbox = computed(() => build.value?.inbox ?? [])
const hasQuestion = computed(() => inbox.value.some((i) => i.kind === 'question' && i.status === 'open'))
// Why the composer is off. An open question does not lock it: the composer answers the question instead.
const composerLocked = computed(() => {
  if (projSuspended.value) return SUSPENDED_TITLE
  if (!canRun.value) return hasQuestion.value ? 'You can not answer for this build.' : 'You can not send changes to this build.'
  if (active.value) return 'Kaisen is working. You can send more once this run finishes.'
  return ''
})
// What to say when someone tries to attach while the composer is locked.
const attachLocked = computed(() => {
  if (projSuspended.value) return SUSPENDED_TITLE
  if (!canRun.value) return 'You can not attach files to this build.'
  if (active.value) return 'You can\'t attach files while Kaisen is working. Attach them once this run finishes.'
  return ''
})
// Why "send a change" actions (Fix it, suggestions) are off.
const feedbackReason = computed(() => composerLocked.value || (hasQuestion.value ? 'Answer Kaisen\'s question first.' : ''))
const firstRun = computed(() => !!build.value && build.value.runs.length === 0 && build.value.status === 'queued')
const out = computed(() => build.value?.output)
const activeRun = computed(() => (active.value ? build.value?.runs.find((r) => r.status === 'running' || r.status === 'queued') ?? null : null))
const settings = ref<KaisenSettings | null>(null)
const hint = computed(() => (build.value ? costHint(build.value.runs, settings.value?.model ?? null, settings.value?.cap_run ?? null) : ''))
async function loadSettings(pid: number) {
  try {
    settings.value = await getKaisenSettings(pid)
  } catch {
    settings.value = null // hint is optional
  }
}
const outForm = reactive(blankOutputForm())
const outErr = reactive<Record<string, string>>({})
const outTest = ref<{ ok: boolean; message: string } | null>(null)
const outDirty = ref(false)
const githubReady = computed(() => out.value?.mode === 'github' && !!out.value.repo)

async function loadApp(b: KaisenBuildDetail) {
  if (!b.app_id) {
    app.value = null
    return
  }
  try {
    app.value = await getApp(b.app_id)
  } catch {
    app.value = null
  }
}

function apply(b: KaisenBuildDetail) {
  const prev = build.value?.status
  if (!outDirty.value || !build.value) {
    Object.assign(outForm, outputToForm(b.output))
    // Loading saved settings is not an edit: the deep watcher below fires after this tick.
    nextTick(() => nextTick(() => { outDirty.value = false }))
  }
  build.value = b
  store.upsert(b)
  if (prev && prev !== b.status) announce.value = `Build ${b.name}: ${statusLook(b.status, 'build').label}.`
  loadApp(b)
}

// Bumped when an action starts and ends, so a poll that began earlier cannot overwrite the action's newer result.
let actSeq = 0
async function load(initial = true) {
  if (initial) loadError.value = ''
  const seq = actSeq
  try {
    const b = await getBuild(buildId.value)
    if (!initial && seq !== actSeq) return
    apply(b)
    notFound.value = false
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound.value = true
    else if (initial || !build.value) loadError.value = e instanceof ApiError ? e.message : "Couldn't load this build."
  }
}

let timer: ReturnType<typeof setInterval> | null = null
function syncPolling() {
  const busyNow = active.value
  if (busyNow && !timer) timer = setInterval(() => load(false), 3000)
  else if (!busyNow && timer) {
    clearInterval(timer)
    timer = null
  }
}
watch(active, syncPolling)
onMounted(async () => {
  await load()
  if (build.value) loadSettings(build.value.project_id)
  syncPolling()
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  timer = null
  stopPushPoll()
})
watch(buildId, () => {
  build.value = null
  app.value = null
  load()
})

async function act(kind: string, fn: () => Promise<KaisenBuildDetail>, ok?: string) {
  actionError.value = ''
  busy.value = kind
  actSeq++
  try {
    const b = await fn()
    actSeq++
    apply(b)
    if (ok) toast.success(ok)
    return b
  } catch (e) {
    actionError.value = e instanceof ApiError ? e.message : 'Something went wrong. Try again.'
  } finally {
    busy.value = ''
  }
}
async function sendChange(text?: string) {
  const t = (text ?? feedback.value).trim()
  if (!t) return
  if (await act('feedback', () => sendFeedback(buildId.value, t))) feedback.value = ''
}
const FIX_TEXT = 'The preview is failing. Read the deploy logs with deploy_preview, find the cause and fix it.'
const TESTS_FIX_TEXT = 'The generated tests are failing. Read the test output with deploy_preview and fix it.'
const testsFailing = computed(() => ['fail', 'timeout'].includes(build.value?.tests?.status ?? ''))
const TEST_LOOK: Record<string, { label: string; tone: string }> = {
  pass: { label: 'Tests pass', tone: 'ok' },
  fail: { label: 'Tests fail', tone: 'err' },
  timeout: { label: 'Tests timed out', tone: 'warn' },
  running: { label: 'Tests running', tone: 'off' },
}
async function fixTests() {
  await act('feedback', () => sendFeedback(buildId.value, TESTS_FIX_TEXT))
}
// Where the live app is, in the words the page uses. 'updating': a new version is rolling out and the old one is still
// live; 'starting': first deploy, nothing live yet; 'live': running the current version; 'other': error, stopped, ...
const deployPhase = computed<'updating' | 'starting' | 'live' | 'other'>(() => {
  const status = app.value?.status ?? build.value?.app?.status ?? build.value?.app_status ?? ''
  if (app.value?.updating ?? build.value?.app?.updating) return 'updating'
  if (status === 'pending' || status === 'deploying') return 'starting'
  if (status === 'running') return 'live'
  return 'other'
})
const appBroken = computed(() => ['error', 'retrying'].includes(app.value?.status ?? build.value?.app_status ?? ''))
async function fixIt() {
  // One Fix it: the app-failure text wins when the app is down (and mentions failing tests too); the tests row
  // only offers its own button when the app is not broken.
  const text = testsFailing.value ? `${FIX_TEXT} The generated tests are failing too; fix them as well.` : FIX_TEXT
  await act('feedback', () => sendFeedback(buildId.value, text))
}
async function dismiss(itemId: number) {
  await act('dismiss', () => dismissInbox(buildId.value, itemId))
}
async function answer(itemId: number, text: string) {
  answerBusy.value = itemId
  const b = await act('answer', () => answerInbox(buildId.value, itemId, text))
  answerBusy.value = null
  if (b) feedback.value = ''
}
watch(outForm, () => { outDirty.value = true; outTest.value = null }, { deep: true })
async function saveOutput() {
  actionError.value = ''
  for (const k of Object.keys(outErr)) delete outErr[k]
  if (outForm.output_mode === 'github' && !outForm.github_repo.trim()) {
    outErr.github_repo = 'Enter a GitHub repository URL.'
    return
  }
  busy.value = 'output'
  try {
    const o = await saveBuildOutput(buildId.value, outputPayload(outForm))
    if (build.value) build.value = { ...build.value, output: o }
    Object.assign(outForm, outputToForm(o))
    await Promise.resolve()
    outDirty.value = false
    outTest.value = null
    toast.success('Output saved.')
  } catch (e) {
    if (e instanceof ApiError) {
      Object.assign(outErr, outputFieldErrors(e.fieldErrors))
      actionError.value = Object.keys(e.fieldErrors).length ? '' : e.message
      if (!Object.keys(e.fieldErrors).length) return
    } else actionError.value = 'Something went wrong. Try again.'
  } finally {
    busy.value = ''
  }
}
async function testOutput() {
  busy.value = 'test'
  outTest.value = null
  try {
    outTest.value = await testBuildOutput(buildId.value)
  } catch (e) {
    outTest.value = { ok: false, message: e instanceof ApiError ? e.message : 'The test could not run.' }
  } finally {
    busy.value = ''
  }
}
const resume = () => act('resume', () => runBuild(buildId.value))
const interruptErr = ref('')
async function interrupt(runId: number) {
  interruptErr.value = ''
  busy.value = 'interrupt'
  try {
    apply(await interruptRun(buildId.value, runId))
  } catch (e) {
    interruptErr.value = e instanceof ApiError ? e.message : 'Something went wrong. Try again.'
  } finally {
    busy.value = ''
  }
}
const deployAgain = () => act('deploy', () => deployBuild(buildId.value))
// ---- async push: POST returns a running job at once; poll it every 2 s (stop on done/error, on leaving, after ~10 min) ----
const pushJob = ref<KaisenPushJob | null>(null)
const pushError = ref('')
const pushRunning = computed(() => pushJob.value?.status === 'running')
let pushTimer: ReturnType<typeof setInterval> | null = null
let pushSince = 0
function stopPushPoll() {
  if (pushTimer) clearInterval(pushTimer)
  pushTimer = null
}
function finishPush(j: KaisenPushJob) {
  stopPushPoll()
  if (j.status === 'done') toast.success('Pushed to GitHub.')
  else toast.error(`Push failed: ${j.message}`)
  load(false)
}
function startPushPoll() {
  if (pushTimer) return
  pushSince = Date.now()
  pushTimer = setInterval(async () => {
    const j = pushJob.value
    if (!j || j.status !== 'running') return stopPushPoll()
    if (Date.now() - pushSince > 10 * 60 * 1000) return stopPushPoll()
    try {
      const n = await getPushJob(buildId.value, j.id)
      pushJob.value = n
      if (n.status !== 'running') finishPush(n)
    } catch {
      // transient; the next tick retries
    }
  }, 2000)
}
// A reload shows the newest job from the build detail; a running one resumes polling.
watch(() => build.value?.push, (j) => {
  if (j && (!pushJob.value || j.id >= pushJob.value.id)) pushJob.value = j
  if (pushJob.value?.status === 'running') startPushPoll()
}, { immediate: true })
async function push() {
  pushError.value = ''
  try {
    pushJob.value = await pushBuild(buildId.value)
    startPushPoll()
  } catch (e) {
    pushError.value = e instanceof ApiError ? e.message : 'Something went wrong. Try again.'
  }
}
const NOTHING_CHANGED = ['token_invalid', 'no_permission', 'not_found', 'non_fast_forward', 'protected_branch', 'empty_repo', 'too_large', 'bad_repo', 'rate_limited']
async function download() {
  actionError.value = ''
  busy.value = 'download'
  try {
    await downloadFile(`/kaisen/builds/${buildId.value}/download`, `${build.value?.name ?? 'build'}.tar.gz`)
  } catch (e) {
    actionError.value = e instanceof ApiError ? e.message : "Couldn't download the code."
  } finally {
    busy.value = ''
  }
}
// "Take offline": delete the build's app but keep the build (files, conversation, runs); Deploy again brings it back.
const confirmOffline = ref(false)
const goingOffline = ref(false)
async function takeOffline() {
  if (!build.value?.app_id) return
  goingOffline.value = true
  try {
    await deleteApp(build.value.app_id)
    toast.success(`${build.value.name} is offline. The code and conversation stay in Kaisen.`)
    app.value = null
    await load(false)
  } catch (e) {
    toast.error(`Couldn't take the app offline: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    goingOffline.value = false
    confirmOffline.value = false
  }
}
async function doDelete() {
  if (!build.value) return
  deleting.value = true
  try {
    await store.remove(build.value.id, alsoApp.value)
    toast.success(`Deleted build ${build.value.name}.`)
    router.push(projectKaisen(build.value.project_id))
  } catch (e) {
    toast.error(`Couldn't delete: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    deleting.value = false
    confirmDelete.value = false
  }
}
</script>

<template>
  <div>
    <main id="main" tabindex="-1" class="page-main">
      <div class="sr-only" role="status">{{ announce }}</div>

      <AppNotFound v-if="notFound" kind="build" />

      <div v-else-if="loadError && !build" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
        <span>{{ loadError }}</span>
        <IconButton label="Retry" @click="load()" />
      </div>

      <div v-else-if="!build" class="space-y-3" aria-busy="true" aria-label="Loading build">
        <div class="h-6 w-48 animate-pulse rounded-sm bg-pico-card" />
        <div class="h-32 animate-pulse rounded-sm bg-pico-card" />
      </div>

      <template v-else>
        <div ref="aboveEl">
          <SuspendedBanner :project="buildProject" kaisen />
          <nav class="meta mb-2" aria-label="Breadcrumb">
            <RouterLink :to="`/projects/${build.project_id}`" class="link">{{ projectLabel(buildProject) || 'Project' }}</RouterLink><span aria-hidden="true"> / </span><RouterLink :to="projectKaisen(build.project_id)" class="link">Kaisen</RouterLink><span aria-hidden="true"> / </span>{{ build.name }}
          </nav>

          <header class="mb-3 flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
                <h1 class="page-title !text-lg break-all">{{ build.name }}</h1>
                <StatusBadge kind="build" :status="build.status" />
                <PreviewBadge />
              </div>
              <p class="meta mt-1">Started {{ relTime(build.created_at, now) }} by {{ you ? 'you' : build.created_by.email }} · <span class="mono">{{ fmtUsd(build.spend.build_usd) }}</span> spent on this build</p>
              <!-- Below 1024px the rail is closed by default: keep the live address one glance away. -->
              <p v-if="build.app_id && build.app_hostname" class="meta mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 lg:hidden"><HostLink :hostname="build.app_hostname" /></p>
            </div>
            <div class="flex shrink-0 items-center gap-2">
              <IconButton label="Kaisen settings" text="Settings" :to="projectKaisen(build.project_id, '/settings')" />
            </div>
          </header>

          <p v-if="auth.user?.features?.kaisen === false" class="alert-warn mb-3">Kaisen is turned off on this instance.</p>
          <p v-if="actionError" role="alert" class="alert-error mb-3 whitespace-pre-wrap">{{ actionError }}</p>
        </div>

        <div class="flex flex-col gap-3 lg:grid lg:grid-cols-[minmax(0,1fr)_20rem] lg:items-start lg:gap-6" :style="{ '--chat-h': chatH + 'px' }">
          <section ref="chatCol" class="h-[var(--chat-h)] min-h-0 min-w-0 lg:border-t lg:border-pico-border lg:pt-2" aria-labelledby="h-convo">
            <h2 id="h-convo" class="sr-only">Conversation</h2>
            <BuildConversation
              v-model="feedback" :build="build" :now="now" :you="you" :summary="build.uploads" :can-pick="!feedbackReason" :can-resume="canRun && !active" :resuming="busy === 'resume'"
              :draft="build.runs.length === 0 && !active && build.status === 'idle'" :starting="busy === 'resume'" :locked="composerLocked" :locked-attach="attachLocked" :can-attach="canRun && !active" :busy="busy === 'feedback'" :hint="hint"
              :can-run="canRun" :active-run="activeRun" :first-run="firstRun" :interrupting="busy === 'interrupt'" :interrupt-error="interruptErr" :answer-busy="answerBusy"
              @submit="(p) => sendChange(p.text)" @answer="answer" @dismiss="dismiss" @interrupt="interrupt" @resume="resume" @start="resume" @open-files="openFiles" @changed="load(false)"
            />
          </section>

          <!-- On a phone the rail is one disclosure above the chat; from 1024px it is a column beside it, always open. -->
          <details class="disc max-lg:order-first max-lg:border-t max-lg:border-pico-border lg:h-[var(--chat-h)] lg:overflow-y-auto" :open="desktop || railOpen" aria-label="Build details" @toggle="onRailToggle">
            <summary class="text-sm font-semibold max-lg:min-h-[44px] lg:hidden">Build details</summary>
            <div class="lg:border-t lg:border-pico-border lg:pt-0">
              <details class="disc sec py-1.5" open aria-labelledby="h-live">
                <summary><h2 id="h-live" class="section-title">Live app and preview</h2></summary>
                <div class="pb-2 pt-1">
                  <template v-if="build.app_id && build.app_hostname">
                    <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <HostLink :hostname="build.app_hostname" />
                      <StatusBadge v-if="deployPhase === 'updating'" status="updating" />
                      <span v-else-if="deployPhase === 'starting'" class="st tone-off" role="status"><span class="dot dot-off" aria-hidden="true" />Starting</span>
                      <StatusBadge v-else-if="app || build.app_status" :status="app?.status ?? build.app_status ?? ''" />
                    </div>
                    <div v-if="appBroken && build.app_id" class="mt-2 flex flex-wrap items-center gap-2">
                      <button type="button" class="btn-primary btn-sm" :disabled="!!feedbackReason || busy === 'feedback'" @click="fixIt">{{ busy === 'feedback' ? 'Sending…' : 'Fix it' }}</button>
                      <span class="help !mt-0">{{ feedbackReason || (testsFailing ? 'Asks Kaisen to read the deploy logs, fix the cause and the failing tests.' : 'Asks Kaisen to read the deploy logs and fix the cause.') }}</span>
                    </div>
                    <div class="mt-3 border-t border-pico-border pt-2" aria-labelledby="h-tests">
                      <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
                        <h3 id="h-tests" class="text-xs font-medium text-zinc-200">Tests</h3>
                        <template v-if="build.tests">
                          <span :class="['st', `tone-${TEST_LOOK[build.tests.status]?.tone ?? 'off'}`]"><span :class="['dot', `dot-${TEST_LOOK[build.tests.status]?.tone ?? 'off'}`]" aria-hidden="true" />{{ TEST_LOOK[build.tests.status]?.label ?? build.tests.status }}</span>
                          <span v-if="build.tests.ran_at" class="meta mono">{{ relTime(build.tests.ran_at, now) }}</span>
                          <button v-if="testsFailing && !active && !appBroken" type="button" class="btn-primary btn-sm" :disabled="!!feedbackReason || busy === 'feedback'" @click="fixTests">{{ busy === 'feedback' ? 'Sending…' : 'Fix it' }}</button>
                        </template>
                        <span v-else class="meta">No test result yet. Add a "test" command in kaisen.json.</span>
                      </div>
                      <details v-if="build.tests && build.tests.tail" class="disc mt-1">
                        <summary class="text-sm">Test output</summary>
                        <pre class="mono mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-pico-card p-3 text-zinc-200 [overflow-wrap:anywhere]" tabindex="0" aria-label="Test output">{{ build.tests.tail }}</pre>
                      </details>
                    </div>
                    <p v-if="deployPhase === 'updating'" class="mt-2 text-sm text-zinc-300" role="status">Updating your app. The current version stays live until the new one is ready.</p>
                    <p v-else-if="deployPhase === 'starting'" class="mt-2 text-sm text-zinc-300" role="status">Starting your app. This usually takes about a minute.</p>
                    <BuildLastDeployment :items="inbox" :now="now" :phase="deployPhase" />
                    <div v-if="app && app.status === 'error'" class="mt-2"><AppStatusNote compact :app="app" /></div>
                    <BuildPreviewPane :hostname="build.app_hostname" :status="app?.status ?? build.app_status ?? ''" />
                    <div class="mt-3 flex flex-wrap gap-2">
                      <RouterLink :to="`/apps/${build.app_id}`" class="btn-secondary btn-sm">App health</RouterLink>
                      <RouterLink :to="`/apps/${build.app_id}/edit`" class="btn-secondary btn-sm">App settings</RouterLink>
                      <button v-if="canRun" type="button" class="btn-secondary btn-sm" :disabled="goingOffline || !app" @click="confirmOffline = true">Take offline</button>
                    </div>
                    <p class="help mt-2">Taking it offline deletes the app (and its database, if it has one) but keeps this build. Deploy again any time; the app gets a new address.</p>
                  </template>
                  <div v-else-if="(build.status === 'done' || build.status === 'error' || build.status === 'stopped') && build.files.length > 0" class="alert-info flex flex-wrap items-center justify-between gap-2">
                    <span>Offline. The code and conversation are kept here.</span>
                    <button type="button" class="btn-secondary btn-sm" :disabled="busy === 'deploy' || !canRun" @click="deployAgain">{{ busy === 'deploy' ? 'Deploying…' : 'Deploy again' }}</button>
                  </div>
                  <p v-else-if="build.status === 'error' || build.status === 'stopped'" class="text-sm text-zinc-300">Not deployed. The build {{ build.status === 'error' ? 'stopped on an error' : 'was stopped' }}. Use Resume in the conversation to continue.</p>
                  <p v-else class="text-sm text-zinc-300">Not deployed yet. Kaisen deploys the app when the first build finishes.</p>
                  <BuildLastDeployment v-if="!(build.app_id && build.app_hostname)" :items="inbox" :now="now" />
                </div>
              </details>

              <details class="disc sec py-1.5" aria-labelledby="h-output">
                <summary><h2 id="h-output" class="section-title">Output</h2></summary>
                <div class="pb-2 pt-1">
                  <p class="help mb-2">"Save output settings" only saves the choices below; it does not download anything. Use "Download code" to get the files. Pushing to GitHub is optional and uses this build's own repository.</p>
                  <form class="space-y-3" novalidate @submit.prevent="saveOutput">
                    <BuildOutputFields v-model="outForm" id-prefix="bo" :token-state="build.output.token" :project-token-set="build.output.project_token_set" :project-id="build.project_id" :disabled="!canRun" :errors="outErr" />
                    <div class="flex flex-wrap gap-2">
                      <button type="submit" class="btn-primary" :disabled="!canRun || !outDirty || busy === 'output'">{{ busy === 'output' ? 'Saving…' : 'Save output settings' }}</button>
                      <button v-if="outForm.output_mode === 'github'" type="button" class="btn-secondary" :disabled="!canRun || outDirty || busy === 'test' || !githubReady" @click="testOutput">{{ busy === 'test' ? 'Testing…' : 'Test' }}</button>
                      <button v-if="githubReady" type="button" class="btn-secondary" :disabled="outDirty || pushRunning || !out?.can_push" @click="push">{{ pushRunning ? 'Pushing…' : 'Push now' }}</button>
                      <button type="button" class="btn-secondary" :disabled="busy === 'download'" @click="download">{{ busy === 'download' ? 'Preparing…' : 'Download code (.tar.gz)' }}</button>
                    </div>
                    <div v-if="pushRunning" class="alert-info" role="status">
                      <p><span class="motion-safe:animate-pulse">Pushing to GitHub…</span> You can leave this page; the push keeps going.</p>
                    </div>
                    <div v-else-if="pushError" class="alert-error" role="alert">
                      <p class="whitespace-pre-wrap">{{ pushError }}</p>
                    </div>
                    <div v-else-if="pushJob?.status === 'done'" class="alert-success" role="status">
                      <p>Pushed. {{ pushJob.message }}</p>
                      <p class="mt-1 flex flex-wrap gap-x-3">
                        <a v-if="pushJob.pr_url" :href="pushJob.pr_url" target="_blank" rel="noopener noreferrer" class="link">Open pull request<span class="sr-only"> (opens in new tab)</span></a>
                        <a v-if="pushJob.url" :href="pushJob.url" target="_blank" rel="noopener noreferrer" class="link">View on GitHub<span class="sr-only"> (opens in new tab)</span></a>
                        <span v-if="pushJob.finished_at" class="meta">{{ relTime(pushJob.finished_at, now) }}</span>
                      </p>
                    </div>
                    <div v-else-if="pushJob?.status === 'error'" class="alert-error" role="alert">
                      <p class="whitespace-pre-wrap">Push failed: {{ pushJob.message }}</p>
                      <p v-if="pushJob.error_code && NOTHING_CHANGED.includes(pushJob.error_code)" class="mt-1">Nothing changed in your repo.</p>
                      <p v-if="pushJob.error_code" class="meta mt-1">Reason: <span class="mono">{{ pushJob.error_code }}</span></p>
                      <p class="mt-2 flex flex-wrap gap-2">
                        <button v-if="githubReady" type="button" class="btn-secondary btn-sm" :disabled="outDirty || !out?.can_push" @click="push">Try again</button>
                        <button v-if="githubReady" type="button" class="btn-secondary btn-sm" :disabled="!canRun || outDirty || busy === 'test'" @click="testOutput">Test connection</button>
                      </p>
                    </div>
                    <p v-if="outDirty" class="help">Save first, then test or push.</p>
                  </form>
                  <div role="status">
                    <p v-if="outTest" :class="[outTest.ok ? 'alert-success' : 'alert-error', 'mt-2']">{{ outTest.message }}</p>
                  </div>
                  <p v-if="githubReady && !outDirty" class="help">Pushes to {{ out?.repo }} on branch {{ out?.branch || 'kaisen/' + build.slug }}.{{ out?.open_pr ? ' Opens a pull request.' : '' }}</p>
                </div>
              </details>

              <details class="disc sec py-1.5" open aria-labelledby="h-spend">
                <summary><h2 id="h-spend" class="section-title">Spend</h2></summary>
                <div class="pb-2 pt-1">
                  <p v-if="!build.spend.cap_build" class="meta">No cap set</p>
                  <div v-else>
                    <div class="flex items-baseline justify-between gap-2 text-sm">
                      <span class="meta">This build</span>
                      <span class="mono text-zinc-300">{{ fmtUsd(build.spend.build_usd) }} of {{ fmtUsd(build.spend.cap_build) }}</span>
                    </div>
                    <div class="mt-1 h-2 overflow-hidden rounded-sm bg-pico-muted" role="progressbar" aria-label="This build spend" aria-valuemin="0" :aria-valuemax="build.spend.cap_build" :aria-valuenow="Math.min(build.spend.build_usd, build.spend.cap_build)">
                      <div :class="['h-full', build.spend.build_usd >= build.spend.cap_build ? 'bg-danger' : 'bg-zinc-400']" :style="{ width: Math.min(100, (build.spend.build_usd / build.spend.cap_build) * 100) + '%' }" />
                    </div>
                  </div>
                  <p class="meta mt-2">Last 30 days, project <span class="mono">{{ fmtUsd(build.spend.last30_usd) }}</span></p>
                </div>
              </details>

              <BuildSecrets :build-id="build.id" :secrets="build.secrets ?? []" :can-edit="canRun" :can-deploy="canRun && !!build.app_id && !active" :deploying="busy === 'deploy'" @changed="(sec) => { if (build) build = { ...build, secrets: sec } }" @deploy="deployAgain" />

              <BuildRestorePoints :build-id="build.id" :now="now" :active="active" :can-run="canRun" :kept="build.snapshots_kept" :can-deploy="canRun && !!build.app_id && !active" :deploying="busy === 'deploy'" :refresh-key="build.runs.length + ':' + active" @restored="load(false)" @deploy="deployAgain" />

              <BuildFiles :build-id="build.id" :files="build.files" />

              <section class="sec-danger" aria-labelledby="h-del">
                <h2 id="h-del" class="section-title">Danger zone</h2>
                <div class="mt-2 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <p class="text-sm text-zinc-300">Delete this build, its files and run history. Spend totals stay in your usage.</p>
                  <IconButton label="Delete build" text="Delete build" danger @click="alsoApp = false; confirmDelete = true" />
                </div>
              </section>
            </div>
          </details>
        </div>
      </template>
    </main>

    <BuildFilesDrawer v-if="build?.uploads" :open="filesOpen" :focus-id="filesFocus" :build-id="build.id" :summary="build.uploads" :can-edit="canRun && !active" :can-deploy="canRun && !!build.app_id && !active" :deploying="busy === 'deploy'" @close="filesOpen = false" @changed="load(false)" @deploy="deployAgain" />
    <DeleteAppDialog v-if="confirmOffline" :app="app" :busy="goingOffline" @confirm="takeOffline" @cancel="confirmOffline = false" />
    <ConfirmDialog
      :open="confirmDelete"
      title="Delete build?"
      message="This removes the build, its files and run history. Spend totals stay in your usage."
      danger
      confirm-label="Delete build"
      :require-text="build?.name"
      :busy="deleting"
      busy-label="Deleting…"
      @confirm="doDelete"
      @cancel="confirmDelete = false"
    >
      <label v-if="build?.app_id" class="mt-3 flex items-center gap-2 text-sm text-zinc-200">
        <input v-model="alsoApp" type="checkbox" class="check" /> Also delete the app ({{ build.name }})
      </label>
    </ConfirmDialog>
  </div>
</template>
