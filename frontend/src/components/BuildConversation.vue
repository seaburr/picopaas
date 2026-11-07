<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AttachComposer from './AttachComposer.vue'
import BuildInbox from './BuildInbox.vue'
import BuildProgress from './BuildProgress.vue'
import BuildRunDetails from './BuildRunDetails.vue'
import StatusBadge from './StatusBadge.vue'
import { listUploads, uploadContentUrl } from '../api/kaisen'
import { relTime } from '../composables/appUi'
import { duration, fmtSize, fmtUsd, RUN_KIND_LABEL } from '../composables/kaisen'
import type { KaisenBuildDetail, KaisenInboxItem, KaisenRun, KaisenUpload, KaisenUploadsSummary } from '../types'

// The conversation is the page: a scrolling thread (yours on the right in a box, Kaisen's across the left as plain
// text) with the composer pinned under it. It fills the height its parent gives it.
const props = defineProps<{
  build: KaisenBuildDetail
  now: number
  you: boolean
  summary?: KaisenUploadsSummary
  canPick: boolean
  canResume: boolean
  resuming: boolean
  locked: string
  canAttach: boolean
  busy: boolean
  hint: string
  modelValue: string
  canRun: boolean
  activeRun?: KaisenRun | null
  firstRun?: boolean // the first run is queued but has no run row yet
  interrupting?: boolean
  interruptError?: string
  answerBusy?: number | null
  draft?: boolean // created as a draft: nothing has run yet
  starting?: boolean
}>()
const emit = defineEmits<{
  'update:modelValue': [v: string]
  submit: [p: { text: string; attachmentIds: number[] }]
  answer: [id: number, text: string]
  dismiss: [id: number]
  interrupt: [runId: number]
  resume: []
  'open-files': [uploadId?: number]
  changed: []
  start: []
}>()
const text = computed({ get: () => props.modelValue, set: (v: string) => emit('update:modelValue', v) })

interface Item { key: string; at: number; iso: string; who: 'you' | 'kaisen'; kind: string; text: string; runIdx: number | null; files: KaisenUpload[]; notice?: KaisenInboxItem }
const ts = (iso: string) => {
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`).getTime()
  return Number.isNaN(d) ? 0 : d
}
// The change text is inside the run's first message (plain text, never HTML); gone once the transcript is trimmed.
function changeText(i: number): string {
  const first = props.build.runs[i].transcript?.find((t) => t.role === 'user')?.text ?? ''
  const m = /<feedback>\n?([\s\S]*?)\n?<\/feedback>/.exec(first)
  return m ? m[1] : ''
}
const runsAsc = computed(() => props.build.runs.map((r, i) => ({ r, i })).sort((a, b) => ts(a.r.started_at) - ts(b.r.started_at)))
// Uploads: files with run_id R belong to the message that started run R; run_id null = not sent yet.
const uploads = ref<KaisenUpload[]>([])
async function loadUploads() {
  try {
    uploads.value = (await listUploads(props.build.id)).uploads
  } catch {
    // chips are an extra; the Files drawer still reports errors
  }
}
onMounted(loadUploads)
watch(() => [props.build.id, props.build.runs.length, props.summary?.count, props.summary?.changed_since_deploy, props.build.status], loadUploads)
const unsent = computed(() => uploads.value.filter((u) => !u.run_id))
const filesOf = (runId: number) => uploads.value.filter((u) => u.run_id === runId)
const dims = (u: KaisenUpload) => (u.width && u.height ? `${u.width}×${u.height} · ` : '')

const NOTICE_KINDS = ['budget', 'refusal', 'error']
const items = computed<Item[]>(() => {
  const b = props.build
  const out: Item[] = [{ key: 'brief', at: ts(b.created_at), iso: b.created_at, who: 'you', kind: 'brief', text: b.brief, runIdx: null, files: [] }]
  runsAsc.value.forEach(({ r, i }, n) => {
    const files = filesOf(r.id)
    if (n === 0 && r.kind === 'build') out[0].files = files
    else if (r.kind === 'feedback') out.push({ key: `c${r.id}`, at: ts(r.started_at) - 1, iso: r.started_at, who: 'you', kind: 'change', text: changeText(i) || 'Sent a change.', runIdx: null, files })
    else if (files.length) out.push({ key: `f${r.id}`, at: ts(r.started_at) - 1, iso: r.started_at, who: 'you', kind: 'files', text: '', runIdx: null, files })
    // The active run is the "working" message at the end of the thread.
    // A run with no reply text of its own reads as: the question it asked, then its status line and details.
    const asked = r.summary ? [] : b.inbox.filter((q) => q.kind === 'question' && ts(q.created_at) >= ts(r.started_at) && (!r.finished_at || ts(q.created_at) <= ts(r.finished_at)))
    const runAt = asked.length ? Math.max(...asked.map((q) => ts(q.created_at))) + 1.5 : ts(r.started_at)
    if (props.activeRun?.id !== r.id) out.push({ key: `r${r.id}`, at: runAt, iso: r.started_at, who: 'kaisen', kind: 'run', text: r.summary ?? '', runIdx: i, files: [] })
  })
  for (const it of b.inbox) {
    if (it.kind === 'question') {
      out.push({ key: `q${it.id}`, at: ts(it.created_at) + 1, iso: it.created_at, who: 'kaisen', kind: 'question', text: it.text, runIdx: null, files: [] })
      if (it.answer) out.push({ key: `a${it.id}`, at: ts(it.created_at) + 2, iso: it.created_at, who: 'you', kind: 'answer', text: it.answer, runIdx: null, files: [] })
    } else if (NOTICE_KINDS.includes(it.kind) && it.status === 'open') {
      out.push({ key: `n${it.id}`, at: ts(it.created_at) + 1, iso: it.created_at, who: 'kaisen', kind: 'notice', text: '', runIdx: null, files: [], notice: it })
    }
  }
  return out.sort((a, b2) => a.at - b2.at)
})

// ---- answering: the oldest open question first, from the same composer ----
const openQuestions = computed(() => props.build.inbox.filter((i) => i.kind === 'question' && i.status === 'open').sort((a, b) => a.id - b.id))
const answering = computed(() => openQuestions.value[0] ?? null)
const remaining = computed(() => Math.max(0, openQuestions.value.length - 1))
const brief = (t: string) => (t.length > 90 ? `${t.slice(0, 90).trimEnd()}…` : t)
function onSubmit(p: { text: string; attachmentIds: number[] }) {
  if (props.answerBusy != null) return // an answer is in flight: never send its text to the next question
  stickNow()
  if (answering.value) emit('answer', answering.value.id, p.text)
  else emit('submit', p)
}

// ---- stick to the bottom like any chat ----
const scroller = ref<HTMLElement | null>(null)
const listEl = ref<HTMLElement | null>(null)
const atBottom = ref(true)
const near = () => {
  const el = scroller.value
  return !el || el.scrollHeight - el.scrollTop - el.clientHeight < 48
}
const reduced = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
function toBottom(smooth = false) {
  const el = scroller.value
  if (el) el.scrollTo({ top: el.scrollHeight, behavior: smooth && !reduced() ? 'smooth' : 'auto' })
}
function stickNow() {
  atBottom.value = true
  nextTick(() => toBottom())
}
// Pinned until the reader scrolls UP. Growth of the content or a smaller viewport never unpins by itself, so a layout
// pass between a programmatic scroll and its scroll event can not lose the pin.
let lastTop = 0
function onScroll() {
  const el = scroller.value
  if (!el) return
  if (near()) atBottom.value = true
  else if (el.scrollTop < lastTop) atBottom.value = false
  lastTop = el.scrollTop
}
// Opening a disclosure or a diff grows the content below the reader: unpin first so the view stays put, then
// re-evaluate once the layout has settled.
function onClickCapture(e: MouseEvent) {
  if (!(e.target as Element | null)?.closest('summary, button')) return
  atBottom.value = false
  requestAnimationFrame(() => requestAnimationFrame(() => { atBottom.value = near() }))
}
let ro: ResizeObserver | null = null
onMounted(() => {
  nextTick(() => toBottom())
  if (typeof ResizeObserver === 'undefined') return
  ro = new ResizeObserver(() => {
    if (atBottom.value) toBottom()
  })
  if (listEl.value) ro.observe(listEl.value)
  if (scroller.value) ro.observe(scroller.value)
})
onBeforeUnmount(() => ro?.disconnect())
watch(() => props.build.id, () => stickNow())
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <div class="relative min-h-0 flex-1">
      <!-- eslint-disable-next-line vuejs-accessibility/click-events-have-key-events, vuejs-accessibility/no-static-element-interactions -- the capture handler only watches clicks that summaries and buttons inside the thread already receive from the keyboard -->
      <div ref="scroller" class="h-full overflow-y-auto pr-1" tabindex="0" role="region" aria-label="Conversation messages" @scroll.passive="onScroll" @click.capture="onClickCapture">
        <ol ref="listEl" class="flex flex-col gap-4 pb-2">
          <li v-for="it in items" :key="it.key" class="min-w-0">
            <!-- yours: a plain raised box on the right -->
            <div v-if="it.who === 'you'" class="flex flex-col items-end">
              <p v-if="it.kind === 'brief' && !you" class="meta mb-0.5">{{ build.created_by.email }}</p>
              <div class="min-w-0 max-w-[75%] rounded-md bg-pico-card px-3 py-2 text-sm text-zinc-100 max-sm:max-w-[88%]">
                <p v-if="it.text" class="whitespace-pre-wrap [overflow-wrap:anywhere]">{{ it.text }}</p>
                <ul v-if="it.files.length" class="flex flex-wrap gap-1.5" :class="it.text ? 'mt-2' : ''" aria-label="Attached files">
                  <li v-for="u in it.files" :key="u.id" class="min-w-0 max-w-full">
                    <button type="button" class="flex max-w-full items-center gap-2 rounded-sm border border-pico-border px-1.5 py-1 text-left hover:bg-pico-bg max-md:min-h-[44px]" :title="u.description || undefined" :aria-label="`Open ${u.name_orig} in Files`" @click="emit('open-files', u.id)">
                      <img v-if="u.kind === 'asset'" :src="uploadContentUrl(build.id, u.id, u.version)" alt="" class="h-7 w-7 shrink-0 rounded-sm bg-pico-bg object-cover" loading="lazy" />
                      <span v-else class="mono flex h-7 w-7 shrink-0 items-center justify-center rounded-sm bg-pico-bg text-[10px] text-zinc-300" aria-hidden="true">{{ u.ext.toUpperCase().slice(0, 4) }}</span>
                      <span class="min-w-0">
                        <span class="block truncate text-xs text-zinc-100">{{ u.name_orig }}</span>
                        <span class="meta block truncate"><span class="mono">{{ dims(u) }}{{ fmtSize(u.size) }}</span> · {{ u.kind === 'asset' ? 'Use in the site' : 'Reference only' }}</span>
                      </span>
                    </button>
                  </li>
                </ul>
              </div>
              <p class="meta mt-0.5 text-[11px]">{{ relTime(it.iso, now) }}</p>
            </div>

            <!-- an open notice (spend cap, refusal, error) -->
            <div v-else-if="it.notice" class="max-w-3xl">
              <BuildInbox :item="it.notice" :can-run="canRun" :spend-cap="build.spend.cap_build" @resume="emit('resume')" @dismiss="emit('dismiss', $event)" />
            </div>

            <!-- Kaisen: plain text across the left -->
            <div v-else class="max-w-3xl min-w-0">
              <p v-if="it.text" class="whitespace-pre-wrap text-sm text-zinc-100 [overflow-wrap:anywhere]">{{ it.text }}</p>
              <template v-if="it.runIdx !== null">
                <div v-if="build.runs[it.runIdx].error" class="mt-1 flex flex-wrap items-start justify-between gap-2">
                  <p class="min-w-0 whitespace-pre-wrap text-sm text-danger-light [overflow-wrap:anywhere]">{{ build.runs[it.runIdx].error }}</p>
                  <button v-if="it.runIdx === 0 && canResume && (build.runs[it.runIdx].status === 'error' || build.runs[it.runIdx].status === 'stopped')" type="button" class="btn-secondary btn-sm" :disabled="resuming" @click="emit('resume')">{{ resuming ? 'Resuming…' : 'Resume' }}</button>
                </div>
                <div v-if="it.runIdx === 0 && build.runs[0].status === 'done' && canPick && build.runs[0].suggestions && build.runs[0].suggestions.length" class="mt-2 flex flex-wrap items-center gap-2">
                  <span class="meta">Next:</span>
                  <button v-for="s in build.runs[0].suggestions" :key="s" type="button" class="btn-secondary btn-sm !h-auto !whitespace-normal py-1 text-left" @click="text = s">{{ s }}</button>
                </div>
                <p class="meta mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  <StatusBadge kind="build" :status="build.runs[it.runIdx].status" />
                  <span class="sr-only">{{ RUN_KIND_LABEL[build.runs[it.runIdx].kind] }} run</span>
                  <span aria-hidden="true">·</span><span class="mono">{{ relTime(build.runs[it.runIdx].started_at, now) }}</span>
                  <template v-if="duration(build.runs[it.runIdx].started_at, build.runs[it.runIdx].finished_at)"><span aria-hidden="true">·</span><span class="mono">{{ duration(build.runs[it.runIdx].started_at, build.runs[it.runIdx].finished_at) }}</span></template>
                  <span aria-hidden="true">·</span><span class="mono">{{ fmtUsd(build.runs[it.runIdx].cost_usd) }}</span>
                </p>
                <details class="disc mt-0.5">
                  <summary class="meta">Run details</summary>
                  <div class="py-2 pl-3.5">
                    <BuildRunDetails :build-id="build.id" :run="build.runs[it.runIdx]" />
                  </div>
                </details>
              </template>
            </div>
          </li>

          <li v-if="activeRun" class="max-w-3xl min-w-0">
            <BuildProgress :run="activeRun" :build-usd="build.spend.build_usd" :cap-build="build.spend.cap_build" :can-run="canRun" :interrupting="interrupting" :interrupt-error="interruptError" @interrupt="emit('interrupt', $event)" />
          </li>
          <li v-else-if="firstRun" class="text-sm text-zinc-200" role="status"><span class="motion-safe:animate-pulse">Your first build is queued.</span></li>
        </ol>
      </div>
      <button v-if="!atBottom" type="button" class="absolute bottom-2 right-3 rounded-md border border-pico-border bg-pico-bg px-2.5 py-0.5 text-xs text-zinc-300 hover:text-white max-md:min-h-[36px]" aria-label="Jump to latest message" @click="atBottom = true; toBottom(true)">Latest</button>
    </div>

    <div v-if="draft" class="alert-info mt-2 shrink-0 space-y-2" role="status">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <span>This build is a draft. Nothing has been built yet.</span>
        <button type="button" class="btn-primary btn-sm" :disabled="starting || !!locked" @click="emit('start')">{{ starting ? 'Starting…' : 'Start building' }}</button>
      </div>
      <div v-if="unsent.length">
        <p class="meta">Sent when you start building:</p>
        <ul class="mt-1 flex flex-wrap gap-1.5" aria-label="Files sent when you start building">
          <li v-for="u in unsent" :key="u.id" class="min-w-0 max-w-full">
            <button type="button" class="flex max-w-full items-center gap-2 rounded-sm border border-pico-border px-1.5 py-1 text-left hover:bg-pico-card max-md:min-h-[44px]" :aria-label="`Open ${u.name_orig} in Files`" @click="emit('open-files', u.id)">
              <img v-if="u.kind === 'asset'" :src="uploadContentUrl(build.id, u.id, u.version)" alt="" class="h-7 w-7 shrink-0 rounded-sm bg-pico-bg object-cover" loading="lazy" />
              <span v-else class="mono flex h-7 w-7 shrink-0 items-center justify-center rounded-sm bg-pico-bg text-[10px] text-zinc-300" aria-hidden="true">{{ u.ext.toUpperCase().slice(0, 4) }}</span>
              <span class="min-w-0"><span class="block truncate text-xs text-zinc-100">{{ u.name_orig }}</span><span class="meta block truncate"><span class="mono">{{ dims(u) }}{{ fmtSize(u.size) }}</span> · {{ u.kind === 'asset' ? 'Use in the site' : 'Reference only' }}</span></span>
            </button>
          </li>
        </ul>
      </div>
    </div>

    <div v-if="!draft" class="mt-2 shrink-0 border-t border-pico-border pt-2">
      <p v-if="answering" class="meta mb-1" role="status">
        Answering Kaisen's question: <span class="[overflow-wrap:anywhere]">"{{ brief(answering.text) }}"</span><template v-if="remaining"> · <span class="mono">{{ remaining }}</span> more waiting after this one</template>
      </p>
      <AttachComposer
        v-model="text" :build-id="build.id" :summary="summary" :label="answering ? 'Your answer to Kaisen' : 'Tell Kaisen what to change'"
        :placeholder="answering ? 'Answer Kaisen\'s question.' : 'Tell Kaisen what to change. Drop, paste or attach files.'"
        :submit-label="answering ? 'Send answer' : 'Send to Kaisen'" :busy="answerBusy != null || (!answering && busy)" :locked="locked" :can-attach="canAttach && !answering"
        :attach-off-reason="answering ? 'Files can\'t go with an answer. Attach them to your next message.' : undefined" :unsent="unsent" :hint="answering ? '' : (hint || 'Kaisen edits the code and redeploys. About a minute.')"
        :rows="2" autogrow :max-rows="7" key-hint
        @submit="onSubmit" @open-files="emit('open-files')" @changed="emit('changed'); loadUploads()"
      />
    </div>
  </div>
</template>
