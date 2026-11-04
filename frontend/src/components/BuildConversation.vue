<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ChevronRight } from 'lucide-vue-next'
import AttachComposer from './AttachComposer.vue'
import BuildRunDetails from './BuildRunDetails.vue'
import StatusBadge from './StatusBadge.vue'
import { listUploads, uploadContentUrl } from '../api/kaisen'
import { relTime } from '../composables/appUi'
import { duration, fmtSize, fmtUsd, RUN_KIND_LABEL } from '../composables/kaisen'
import type { KaisenBuildDetail, KaisenUpload, KaisenUploadsSummary } from '../types'

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
  draft?: boolean // created as a draft: nothing has run yet
  starting?: boolean
}>()
const emit = defineEmits<{
  'update:modelValue': [v: string]
  submit: [p: { text: string; attachmentIds: number[] }]
  resume: []
  'open-files': [uploadId?: number]
  changed: []
  start: []
}>()
const text = computed({ get: () => props.modelValue, set: (v: string) => emit('update:modelValue', v) })

interface Item { key: string; at: number; who: 'you' | 'kaisen'; kind: string; text: string; runIdx: number | null; files: KaisenUpload[] }
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
    // tiles are an extra; the Files drawer still reports errors
  }
}
onMounted(loadUploads)
watch(() => [props.build.id, props.build.runs.length, props.summary?.count, props.summary?.changed_since_deploy, props.build.status], loadUploads)
const unsent = computed(() => uploads.value.filter((u) => !u.run_id))
const filesOf = (runId: number) => uploads.value.filter((u) => u.run_id === runId)
const dims = (u: KaisenUpload) => (u.width && u.height ? `${u.width}×${u.height} · ` : '')

const items = computed<Item[]>(() => {
  const out: Item[] = [{ key: 'brief', at: ts(props.build.created_at), who: 'you', kind: 'brief', text: props.build.brief, runIdx: null, files: [] }]
  runsAsc.value.forEach(({ r, i }, n) => {
    const files = filesOf(r.id)
    if (n === 0 && r.kind === 'build') out[0].files = files
    else if (r.kind === 'feedback') out.push({ key: `c${r.id}`, at: ts(r.started_at) - 1, who: 'you', kind: 'change', text: changeText(i) || 'Sent a change.', runIdx: null, files })
    else if (files.length) out.push({ key: `f${r.id}`, at: ts(r.started_at) - 1, who: 'you', kind: 'files', text: '', runIdx: null, files })
    out.push({ key: `r${r.id}`, at: ts(r.started_at), who: 'kaisen', kind: 'run', text: r.summary ?? '', runIdx: i, files: [] })
  })
  for (const it of props.build.inbox) {
    if (it.kind !== 'question') continue
    out.push({ key: `q${it.id}`, at: ts(it.created_at) + 1, who: 'kaisen', kind: 'question', text: it.text, runIdx: null, files: [] })
    if (it.answer) out.push({ key: `a${it.id}`, at: ts(it.created_at) + 2, who: 'you', kind: 'answer', text: it.answer, runIdx: null, files: [] })
  }
  return out.sort((a, b) => a.at - b.at)
})
const open = ref<Record<string, boolean>>({})
</script>

<template>
  <section class="card card-pad" aria-labelledby="h-convo">
    <h2 id="h-convo" class="section-title mb-3">Conversation</h2>
    <ol class="space-y-3">
      <li v-for="it in items" :key="it.key" class="flex gap-2">
        <span class="mono mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[10px]" :class="it.who === 'you' ? 'bg-pico-card text-zinc-200' : 'bg-accent text-white'" aria-hidden="true">{{ it.who === 'you' ? 'Y' : 'K' }}</span>
        <div class="min-w-0 flex-1">
          <p class="meta">
            <span class="font-medium text-zinc-200">{{ it.who === 'you' ? (it.kind === 'brief' && !you ? build.created_by.email : 'You') : 'Kaisen' }}</span>
            <template v-if="it.kind === 'brief'"> · {{ relTime(build.created_at, now) }} · brief</template>
            <template v-else-if="it.kind === 'change'"> · change</template>
            <template v-else-if="it.kind === 'answer'"> · answer</template>
            <template v-else-if="it.kind === 'files'"> · files</template>
            <template v-else-if="it.kind === 'question'"> · question</template>
            <template v-else-if="it.runIdx !== null">
              · run #{{ build.runs[it.runIdx].id }} <StatusBadge kind="build" :status="build.runs[it.runIdx].status" />
              <span class="mono ml-1"> {{ relTime(build.runs[it.runIdx].started_at, now) }}<template v-if="duration(build.runs[it.runIdx].started_at, build.runs[it.runIdx].finished_at)"> · {{ duration(build.runs[it.runIdx].started_at, build.runs[it.runIdx].finished_at) }}</template> · {{ fmtUsd(build.runs[it.runIdx].cost_usd) }}</span>
              <span class="sr-only"> {{ RUN_KIND_LABEL[build.runs[it.runIdx].kind] }}</span>
            </template>
          </p>
          <p v-if="it.text" class="whitespace-pre-wrap break-words text-sm text-zinc-200">{{ it.text }}</p>
          <ul v-if="it.files.length" class="mt-1 flex flex-wrap gap-2" aria-label="Attached files">
            <li v-for="u in it.files" :key="u.id">
              <button type="button" class="flex max-w-full items-center gap-2 rounded-md border border-pico-border bg-pico-card px-2 py-1 text-left hover:bg-pico-panel" :aria-label="`Open ${u.name_orig} in Files`" @click="emit('open-files', u.id)">
                <img v-if="u.kind === 'asset'" :src="uploadContentUrl(build.id, u.id, u.version)" alt="" class="h-8 w-8 shrink-0 rounded-sm bg-pico-bg object-cover" loading="lazy" />
                <span v-else class="mono flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-pico-bg text-xs text-zinc-300" aria-hidden="true">{{ u.ext.toUpperCase().slice(0, 4) }}</span>
                <span class="min-w-0">
                  <span class="block break-all text-sm text-zinc-100">{{ u.name_orig }}</span>
                  <span class="meta block">{{ dims(u) }}{{ fmtSize(u.size) }} · {{ u.kind === 'asset' ? 'Use in the site' : 'Reference only' }}</span>
                  <span v-if="u.description" class="meta block break-words">{{ u.description }}</span>
                </span>
              </button>
            </li>
          </ul>
          <details v-if="it.runIdx !== null" class="group mt-1 rounded-md border border-pico-border" :open="open[it.key] ?? it.runIdx === 0" @toggle="open[it.key] = ($event.target as HTMLDetailsElement).open">
            <summary class="flex cursor-pointer items-center gap-1 px-3 py-1.5 text-sm text-zinc-300">
              <ChevronRight :size="14" class="transition-transform group-open:rotate-90" aria-hidden="true" />Run details: changes, review, transcript
            </summary>
            <div class="border-t border-pico-border px-3 py-2">
              <BuildRunDetails :build-id="build.id" :run="build.runs[it.runIdx]" :is-latest="it.runIdx === 0" :can-pick="canPick" :can-resume="canResume" :resuming="resuming" @pick="text = $event" @resume="emit('resume')" />
            </div>
          </details>
        </div>
      </li>
    </ol>

    <div v-if="draft" class="alert-info mt-3 space-y-2" role="status">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <span>This build is a draft. Nothing has been built yet.</span>
        <button type="button" class="btn-primary btn-sm" :disabled="starting || !!locked" @click="emit('start')">{{ starting ? 'Starting…' : 'Start building' }}</button>
      </div>
      <div v-if="unsent.length">
        <p class="meta">Sent when you start building:</p>
        <ul class="mt-1 flex flex-wrap gap-2" aria-label="Files sent when you start building">
          <li v-for="u in unsent" :key="u.id">
            <button type="button" class="flex max-w-full items-center gap-2 rounded-md border border-pico-border bg-pico-card px-2 py-1 text-left hover:bg-pico-panel" :aria-label="`Open ${u.name_orig} in Files`" @click="emit('open-files', u.id)">
              <img v-if="u.kind === 'asset'" :src="uploadContentUrl(build.id, u.id, u.version)" alt="" class="h-8 w-8 shrink-0 rounded-sm bg-pico-bg object-cover" loading="lazy" />
              <span v-else class="mono flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-pico-bg text-xs text-zinc-300" aria-hidden="true">{{ u.ext.toUpperCase().slice(0, 4) }}</span>
              <span class="min-w-0"><span class="block break-all text-sm text-zinc-100">{{ u.name_orig }}</span><span class="meta block">{{ dims(u) }}{{ fmtSize(u.size) }} · {{ u.kind === 'asset' ? 'Use in the site' : 'Reference only' }}</span></span>
            </button>
          </li>
        </ul>
      </div>
    </div>
    <div v-if="!draft" class="mt-4 border-t border-pico-border pt-3">
      <AttachComposer
        v-model="text" :build-id="build.id" :summary="summary" label="Tell Kaisen what to change" placeholder="Tell Kaisen what to change. Drop, paste or attach files."
        submit-label="Send to Kaisen" :busy="busy" :locked="locked" :can-attach="canAttach" :unsent="unsent" :hint="hint || 'Kaisen edits the code and redeploys. About a minute.'"
        @submit="emit('submit', $event)" @open-files="emit('open-files')" @changed="emit('changed'); loadUploads()"
      />
    </div>
  </section>
</template>
