<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Download, RotateCw, Settings, Trash2 } from 'lucide-vue-next'
import AppStatusNote from '../components/AppStatusNote.vue'
import BuildFiles from '../components/BuildFiles.vue'
import BuildOutputFields from '../components/BuildOutputFields.vue'
import BuildInbox from '../components/BuildInbox.vue'
import BuildProgress from '../components/BuildProgress.vue'
import BuildSecrets from '../components/BuildSecrets.vue'
import BuildRuns from '../components/BuildRuns.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DeleteAppDialog from '../components/DeleteAppDialog.vue'
import HostLink from '../components/HostLink.vue'
import IconButton from '../components/IconButton.vue'
import NavBar from '../components/NavBar.vue'
import PreviewBadge from '../components/PreviewBadge.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError, downloadFile } from '../api/client'
import { getKaisenSettings, answerInbox, deployBuild, dismissInbox, getBuild, pushBuild, runBuild, saveBuildOutput, sendFeedback, testBuildOutput } from '../api/kaisen'
import { deleteApp, getApp } from '../api/apps'
import { relTime, useNow } from '../composables/appUi'
import { blankOutputForm, costHint, fmtUsd, isActive, outputFieldErrors, outputPayload, outputToForm } from '../composables/kaisen'
import { useAuthStore } from '../stores/auth'
import { useKaisenStore } from '../stores/kaisen'
import { useToastStore } from '../stores/toast'
import type { App, KaisenBuildDetail, KaisenSettings } from '../types'

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
const busy = ref('') // '' | 'feedback' | 'answer' | 'deploy' | 'push' | 'resume' | 'download'
const answerBusy = ref<number | null>(null)
const feedback = ref('')
const pushedUrl = ref('')
const confirmDelete = ref(false)
const alsoApp = ref(false)
const deleting = ref(false)

const buildId = computed(() => Number(props.id))
const active = computed(() => !!build.value && isActive(build.value.status))
const canRun = computed(() => !!build.value?.can_run && auth.user?.features?.kaisen !== false)
const you = computed(() => build.value?.created_by.id === auth.user?.id)
const inbox = computed(() => build.value?.inbox ?? [])
const hasQuestion = computed(() => inbox.value.some((i) => i.kind === 'question' && i.status === 'open'))
const feedbackReason = computed(() => {
  if (!canRun.value) return 'You can not send changes to this build.'
  if (active.value) return 'Kaisen is working. You can send more once this run finishes.'
  if (hasQuestion.value) return 'Answer Kaisen\'s question first.'
  return ''
})
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
  if (!outDirty.value || !build.value) Object.assign(outForm, outputToForm(b.output))
  build.value = b
  store.upsert(b)
  if (prev && prev !== b.status) announce.value = `Build ${b.name} is now ${b.status}.`
  loadApp(b)
}

async function load(initial = true) {
  if (initial) loadError.value = ''
  try {
    apply(await getBuild(buildId.value))
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
})
watch(buildId, () => {
  build.value = null
  app.value = null
  load()
})

async function act(kind: string, fn: () => Promise<KaisenBuildDetail>, ok?: string) {
  actionError.value = ''
  busy.value = kind
  try {
    const b = await fn()
    apply(b)
    if (ok) toast.success(ok)
    return b
  } catch (e) {
    actionError.value = e instanceof ApiError ? e.message : 'Something went wrong. Try again.'
  } finally {
    busy.value = ''
  }
}
async function sendChange() {
  const t = feedback.value.trim()
  if (!t) return
  if (await act('feedback', () => sendFeedback(buildId.value, t))) feedback.value = ''
}
const FIX_TEXT = 'The preview is failing. Read the deploy logs with deploy_preview, find the cause and fix it.'
const TESTS_FIX_TEXT = 'The generated tests are failing. Read the test output with deploy_preview and fix it.'
const testsFailing = computed(() => ['fail', 'timeout'].includes(build.value?.tests?.status ?? ''))
const TEST_LOOK: Record<string, { label: string; cls: string }> = {
  pass: { label: 'Tests pass', cls: 'badge-ok' },
  fail: { label: 'Tests fail', cls: 'badge-error' },
  timeout: { label: 'Tests timed out', cls: 'badge-warn' },
  running: { label: 'Tests running', cls: 'badge-info' },
}
async function fixTests() {
  await act('feedback', () => sendFeedback(buildId.value, TESTS_FIX_TEXT))
}
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
  await act('answer', () => answerInbox(buildId.value, itemId, text))
  answerBusy.value = null
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
const deployAgain = () => act('deploy', () => deployBuild(buildId.value))
async function push() {
  const b = await act('push', () => pushBuild(buildId.value), 'Pushed to GitHub.')
  if (b?.output.pr_url) pushedUrl.value = b.output.pr_url
}
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
    router.push('/kaisen')
  } catch (e) {
    toast.error(`Couldn't delete: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    deleting.value = false
    confirmDelete.value = false
  }
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />

    <main id="main" tabindex="-1" class="page-main">
      <div class="sr-only" role="status">{{ announce }}</div>

      <div v-if="notFound" class="card card-pad">
        <h1 class="page-title">Build not found</h1>
        <p class="mt-1 text-sm text-zinc-300">This build doesn't exist or was deleted.</p>
        <RouterLink to="/kaisen" class="btn-secondary mt-4">Back to Kaisen</RouterLink>
      </div>

      <div v-else-if="loadError && !build" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
        <span>{{ loadError }}</span>
        <IconButton :icon="RotateCw" label="Retry" @click="load()" />
      </div>

      <div v-else-if="!build" class="space-y-3" aria-busy="true" aria-label="Loading build">
        <div class="h-6 w-48 animate-pulse rounded-sm bg-pico-panel" />
        <div class="h-32 animate-pulse rounded-sm bg-pico-panel" />
      </div>

      <template v-else>
        <nav class="meta mb-2" aria-label="Breadcrumb">
          <RouterLink to="/kaisen" class="link">Kaisen</RouterLink><span aria-hidden="true"> / </span>{{ build.name }}
        </nav>

        <header class="mb-3 flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2">
              <h1 class="page-title break-all">{{ build.name }}</h1>
              <StatusBadge kind="build" :status="build.status" />
              <PreviewBadge />
            </div>
            <p class="meta mt-1">Started {{ relTime(build.created_at, now) }} by {{ you ? 'you' : build.created_by.email }} · <span class="mono">{{ fmtUsd(build.spend.build_usd) }}</span> spent on this build</p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <IconButton :icon="Settings" label="Kaisen settings" to="/kaisen/settings" />
            <IconButton :icon="Trash2" label="Delete build" danger @click="alsoApp = false; confirmDelete = true" />
          </div>
        </header>

        <p v-if="auth.user?.features?.kaisen === false" class="alert-warn mb-3">Kaisen is turned off on this instance.</p>
        <p v-if="actionError" role="alert" class="alert-error mb-3 whitespace-pre-wrap">{{ actionError }}</p>

        <div class="space-y-3">
          <section class="card card-pad" aria-labelledby="h-live">
            <h2 id="h-live" class="section-title mb-2">Live app</h2>
            <template v-if="build.app_id && build.app_hostname">
              <div class="flex flex-wrap items-center gap-2">
                <HostLink :hostname="build.app_hostname" />
                <StatusBadge v-if="app || build.app_status" :status="app?.status ?? build.app_status ?? ''" />
              </div>
              <div v-if="appBroken && build.app_id" class="mt-2 flex flex-wrap items-center gap-2">
                <button type="button" class="btn-primary btn-sm" :disabled="!!feedbackReason || busy === 'feedback'" @click="fixIt">{{ busy === 'feedback' ? 'Sending…' : 'Fix it' }}</button>
                <span class="help">{{ feedbackReason || (testsFailing ? 'Asks Kaisen to read the deploy logs, fix the cause and the failing tests.' : 'Asks Kaisen to read the deploy logs and fix the cause.') }}</span>
              </div>
              <div class="mt-3 border-t border-pico-border pt-2" aria-labelledby="h-tests">
                <div class="flex flex-wrap items-center gap-2">
                  <h3 id="h-tests" class="label !mb-0">Tests</h3>
                  <template v-if="build.tests">
                    <span :class="['badge', TEST_LOOK[build.tests.status]?.cls ?? 'badge-neutral']">{{ TEST_LOOK[build.tests.status]?.label ?? build.tests.status }}</span>
                    <span v-if="build.tests.ran_at" class="meta mono">{{ relTime(build.tests.ran_at, now) }}</span>
                    <button v-if="testsFailing && !active && !appBroken" type="button" class="btn-primary btn-sm" :disabled="!!feedbackReason || busy === 'feedback'" @click="fixTests">{{ busy === 'feedback' ? 'Sending…' : 'Fix it' }}</button>
                  </template>
                  <span v-else class="meta">No test result yet. Add a "test" command in kaisen.json.</span>
                </div>
                <details v-if="build.tests && build.tests.tail" class="mt-1">
                  <summary class="cursor-pointer text-sm">Test output</summary>
                  <pre class="mono mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-pico-bg p-3 text-xs text-zinc-200" tabindex="0" aria-label="Test output">{{ build.tests.tail }}</pre>
                </details>
              </div>
              <div v-if="app && ['pending', 'deploying', 'error'].includes(app.status)" class="mt-2"><AppStatusNote compact :app="app" /></div>
              <div class="mt-3 flex flex-col gap-2 sm:flex-row">
                <RouterLink :to="`/apps/${build.app_id}`" class="btn-secondary">App health</RouterLink>
                <RouterLink :to="`/apps/${build.app_id}/edit`" class="btn-secondary">App settings</RouterLink>
                <button v-if="canRun" type="button" class="btn-secondary sm:ml-auto" :disabled="goingOffline || !app" @click="confirmOffline = true">Take offline</button>
              </div>
              <p class="help mt-2">Taking it offline deletes the app (and its database, if it has one) but keeps this build. Deploy again any time; the app gets a new address.</p>
            </template>
            <div v-else-if="(build.status === 'done' || build.status === 'error' || build.status === 'stopped') && build.files.length > 0" class="alert-info flex flex-wrap items-center justify-between gap-2">
              <span>Offline. The code and conversation are kept here.</span>
              <button type="button" class="btn-secondary btn-sm" :disabled="busy === 'deploy' || !canRun" @click="deployAgain">{{ busy === 'deploy' ? 'Deploying…' : 'Deploy again' }}</button>
            </div>
            <p v-else class="text-sm text-zinc-300">Not deployed yet. Kaisen deploys the app when the first build finishes.</p>
          </section>

          <BuildInbox :items="inbox" :can-run="canRun" :busy-id="answerBusy" :spend-cap="build.spend.cap_build" @answer="answer" @resume="resume" @dismiss="dismiss" />

          <BuildProgress v-if="activeRun" :run="activeRun" :build-usd="build.spend.build_usd" :cap-build="build.spend.cap_build" />
          <p v-if="firstRun" class="help">Your first build is queued.</p>
          <section v-else class="card card-pad" aria-labelledby="h-change">
            <h2 id="h-change" class="section-title mb-2">Tell Kaisen what to change</h2>
            <form @submit.prevent="sendChange">
              <textarea v-model="feedback" class="input" rows="3" aria-labelledby="h-change" :disabled="!!feedbackReason" />
              <p class="help">{{ feedbackReason || 'Kaisen edits the code and redeploys. About a minute.' }}</p>
              <div class="mt-2 flex flex-wrap items-center justify-end gap-x-3 gap-y-1">
                <span v-if="hint && !feedbackReason" class="help">{{ hint }}</span>
                <button type="submit" class="btn-primary" :disabled="!!feedbackReason || busy === 'feedback' || !feedback.trim()">{{ busy === 'feedback' ? 'Sending…' : 'Send to Kaisen' }}</button>
              </div>
            </form>
          </section>

          <BuildRuns :runs="build.runs" :now="now" :can-pick="!feedbackReason" @pick="feedback = $event" />
          <BuildFiles :build-id="build.id" :files="build.files" />

          <BuildSecrets :build-id="build.id" :secrets="build.secrets ?? []" :can-edit="canRun" :can-deploy="canRun && !!build.app_id && !active" :deploying="busy === 'deploy'" @changed="(sec) => { if (build) build = { ...build, secrets: sec } }" @deploy="deployAgain" />

          <section class="card card-pad" aria-labelledby="h-output">
            <h2 id="h-output" class="section-title mb-2">Output</h2>
            <p class="help mb-2">"Save output settings" only saves the choices below; it does not download anything. Use "Download code" to get the files. Pushing to GitHub is optional and uses this build's own repository.</p>
            <form class="space-y-3" novalidate @submit.prevent="saveOutput">
              <BuildOutputFields v-model="outForm" id-prefix="bo" :token-state="build.output.token" :project-token-set="build.output.project_token_set" :disabled="!canRun" :errors="outErr" />
              <div class="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
                <button type="submit" class="btn-primary" :disabled="!canRun || !outDirty || busy === 'output'">{{ busy === 'output' ? 'Saving…' : 'Save output settings' }}</button>
                <button v-if="outForm.output_mode === 'github'" type="button" class="btn-secondary" :disabled="!canRun || outDirty || busy === 'test' || !githubReady" @click="testOutput">{{ busy === 'test' ? 'Testing…' : 'Test' }}</button>
                <button v-if="githubReady" type="button" class="btn-secondary" :disabled="outDirty || busy === 'push' || !out?.can_push" @click="push">{{ busy === 'push' ? 'Pushing…' : 'Push now' }}</button>
                <button type="button" class="btn-secondary" :disabled="busy === 'download'" @click="download"><Download :size="16" aria-hidden="true" /> {{ busy === 'download' ? 'Preparing…' : 'Download code (.tar.gz)' }}</button>
              </div>
              <p v-if="outDirty" class="help">Save first, then test or push.</p>
            </form>
            <div role="status">
              <p v-if="outTest" :class="[outTest.ok ? 'alert-success' : 'alert-error', 'mt-2']">{{ outTest.message }}</p>
            </div>
            <p v-if="githubReady && !outDirty" class="help">Pushes to {{ out?.repo }} on branch {{ out?.branch || 'kaisen/' + build.slug }}.{{ out?.open_pr ? ' Opens a pull request.' : '' }}</p>
            <p v-if="pushedUrl" class="alert-success mt-2" role="status">Pushed. <a :href="pushedUrl" target="_blank" rel="noopener noreferrer" class="link">View on GitHub<span class="sr-only"> (opens in new tab)</span></a></p>
          </section>

          <section class="card card-pad" aria-labelledby="h-spend">
            <h2 id="h-spend" class="section-title mb-2">Spend</h2>
            <p v-if="!build.spend.cap_build" class="meta">No cap set</p>
            <div v-else>
              <div class="flex items-baseline justify-between gap-2 text-sm">
                <span class="meta">This build</span>
                <span class="mono text-zinc-300">{{ fmtUsd(build.spend.build_usd) }} of {{ fmtUsd(build.spend.cap_build) }}</span>
              </div>
              <div class="mt-1 h-1.5 overflow-hidden rounded-sm bg-pico-card" role="progressbar" aria-label="This build spend" aria-valuemin="0" :aria-valuemax="build.spend.cap_build" :aria-valuenow="Math.min(build.spend.build_usd, build.spend.cap_build)">
                <div :class="['h-full', build.spend.build_usd >= build.spend.cap_build ? 'bg-danger' : 'bg-accent']" :style="{ width: Math.min(100, (build.spend.build_usd / build.spend.cap_build) * 100) + '%' }" />
              </div>
            </div>
            <p class="meta mt-2">This month, project <span class="mono">{{ fmtUsd(build.spend.month_usd) }}</span></p>
          </section>
        </div>
      </template>
    </main>

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
