<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { RotateCw, Settings, Trash2 } from 'lucide-vue-next'
import AppStatusNote from '../components/AppStatusNote.vue'
import BuildFiles from '../components/BuildFiles.vue'
import BuildInbox from '../components/BuildInbox.vue'
import BuildRuns from '../components/BuildRuns.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import HostLink from '../components/HostLink.vue'
import IconButton from '../components/IconButton.vue'
import NavBar from '../components/NavBar.vue'
import PreviewBadge from '../components/PreviewBadge.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError, downloadFile } from '../api/client'
import { answerInbox, deployBuild, getBuild, pushBuild, runBuild, sendFeedback } from '../api/kaisen'
import { getApp } from '../api/apps'
import { relTime, useNow } from '../composables/appUi'
import { fmtUsd, isActive } from '../composables/kaisen'
import { useAuthStore } from '../stores/auth'
import { useKaisenStore } from '../stores/kaisen'
import { useToastStore } from '../stores/toast'
import type { App, KaisenBuildDetail } from '../types'

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
const dismissed = ref<Set<number>>(new Set())
const confirmDelete = ref(false)
const alsoApp = ref(false)
const deleting = ref(false)

const buildId = computed(() => Number(props.id))
const active = computed(() => !!build.value && isActive(build.value.status))
const canRun = computed(() => !!build.value?.can_run && auth.user?.features?.kaisen !== false)
const you = computed(() => build.value?.created_by.id === auth.user?.id)
const inbox = computed(() => (build.value?.inbox ?? []).filter((i) => !dismissed.value.has(i.id)))
const hasQuestion = computed(() => inbox.value.some((i) => i.kind === 'question' && i.status === 'open'))
const feedbackReason = computed(() => {
  if (!canRun.value) return 'You can not send changes to this build.'
  if (active.value) return 'Kaisen is working. You can send more once this run finishes.'
  if (hasQuestion.value) return 'Answer Kaisen\'s question first.'
  return ''
})
const firstRun = computed(() => !!build.value && build.value.runs.length === 0 && build.value.status === 'queued')
const out = computed(() => build.value?.output)
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
async function answer(itemId: number, text: string) {
  answerBusy.value = itemId
  await act('answer', () => answerInbox(buildId.value, itemId, text))
  answerBusy.value = null
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

    <main id="main" tabindex="-1" class="mx-auto max-w-6xl px-4 py-4 sm:px-6">
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
              <div v-if="app && ['pending', 'deploying', 'error'].includes(app.status)" class="mt-2"><AppStatusNote compact :app="app" /></div>
              <div class="mt-3 flex flex-col gap-2 sm:flex-row">
                <RouterLink :to="`/apps/${build.app_id}`" class="btn-secondary">App health</RouterLink>
                <RouterLink :to="`/apps/${build.app_id}/edit`" class="btn-secondary">App settings</RouterLink>
              </div>
            </template>
            <div v-else-if="(build.status === 'done' || build.status === 'error' || build.status === 'stopped') && build.files.length > 0" class="alert-warn flex flex-wrap items-center justify-between gap-2">
              <span>The app was deleted.</span>
              <button type="button" class="btn-secondary btn-sm" :disabled="busy === 'deploy' || !canRun" @click="deployAgain">{{ busy === 'deploy' ? 'Deploying…' : 'Deploy again' }}</button>
            </div>
            <p v-else class="text-sm text-zinc-300">Not deployed yet. Kaisen deploys the app when the first build finishes.</p>
          </section>

          <BuildInbox :items="inbox" :can-run="canRun" :busy-id="answerBusy" :spend-cap="build.spend.cap_build" @answer="answer" @resume="resume" @dismiss="(id) => dismissed.add(id)" />

          <p v-if="firstRun" class="help">Your first build is queued.</p>
          <section v-else class="card card-pad" aria-labelledby="h-change">
            <h2 id="h-change" class="section-title mb-2">Tell Kaisen what to change</h2>
            <form @submit.prevent="sendChange">
              <textarea v-model="feedback" class="input" rows="3" aria-labelledby="h-change" :disabled="!!feedbackReason" />
              <p class="help">{{ feedbackReason || 'Kaisen edits the code and redeploys. About a minute.' }}</p>
              <div class="mt-2 flex justify-end">
                <button type="submit" class="btn-primary" :disabled="!!feedbackReason || busy === 'feedback' || !feedback.trim()">{{ busy === 'feedback' ? 'Sending…' : 'Send to Kaisen' }}</button>
              </div>
            </form>
          </section>

          <BuildRuns :runs="build.runs" :now="now" />
          <BuildFiles :build-id="build.id" :files="build.files" />

          <section class="card card-pad" aria-labelledby="h-output">
            <h2 id="h-output" class="section-title mb-2">Output</h2>
            <div class="flex flex-col gap-2 sm:flex-row">
              <button type="button" class="btn-secondary" :disabled="busy === 'download'" @click="download">{{ busy === 'download' ? 'Preparing…' : 'Download .tar.gz' }}</button>
              <button v-if="githubReady" type="button" class="btn-secondary" :disabled="busy === 'push' || !out?.can_push" @click="push">{{ busy === 'push' ? 'Pushing…' : 'Push to GitHub' }}</button>
              <button v-else type="button" class="btn-secondary" disabled title="Set up GitHub in Kaisen settings">Push to GitHub</button>
            </div>
            <p v-if="githubReady" class="help">Pushes to {{ out?.repo }} on branch {{ out?.branch }}. Opens a pull request.</p>
            <p v-else class="help">GitHub is optional. <RouterLink to="/kaisen/settings" class="link">Set it up in Kaisen settings</RouterLink></p>
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
