<script setup lang="ts">
import { ref } from 'vue'
import { ChevronRight, FileDiff } from 'lucide-vue-next'
import BuildDiffPanel from './BuildDiffPanel.vue'
import { getRunDiff } from '../api/kaisen'
import CopyButton from './CopyButton.vue'
import StatusBadge from './StatusBadge.vue'
import { relTime } from '../composables/appUi'
import { duration, fmtUsd, RUN_KIND_LABEL } from '../composables/kaisen'
import type { KaisenRun } from '../types'

defineProps<{ buildId: number; runs: KaisenRun[]; now: number; canPick?: boolean }>()
const showDiff = ref<number | null>(null)
const emit = defineEmits<{ pick: [text: string] }>()

// Plain text only: prefix each turn with its role; never rendered as HTML.
const VERDICT: Record<string, { label: string; cls: string }> = {
  approve: { label: 'Approved', cls: 'badge-ok' },
  changes: { label: 'Changes requested', cls: 'badge-warn' },
  skipped: { label: 'Review skipped', cls: 'badge-neutral' },
}
const isReviewer = (t: { role: string; text: string }) => t.role === 'user' && t.text.startsWith('[reviewer]')
const body = (t: { role: string; text: string }) => (isReviewer(t) ? t.text.replace(/^\[reviewer\]\s*/, '') : t.text)
const label = (t: { role: string; text: string }) => (isReviewer(t) ? 'Reviewer' : t.role)
const text = (r: KaisenRun) => (r.transcript ?? []).map((t) => `${label(t)}: ${body(t)}`).join('\n\n')
</script>

<template>
  <section class="card card-pad" aria-labelledby="h-runs">
    <h2 id="h-runs" class="section-title mb-2">Runs ({{ runs.length }})</h2>
    <p v-if="runs.length === 0" class="text-sm text-zinc-300">No runs yet.</p>
    <div v-else class="space-y-2">
      <details v-for="(r, i) in runs" :key="r.id" class="group rounded-md border border-pico-border" :open="i === 0">
        <summary class="flex cursor-pointer flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2 text-sm">
          <ChevronRight :size="14" class="transition-transform group-open:rotate-90" aria-hidden="true" />
          <span class="mono">#{{ r.id }}</span>
          <span>{{ RUN_KIND_LABEL[r.kind] ?? r.kind }}</span>
          <StatusBadge kind="build" :status="r.status" />
          <span class="meta mono">{{ relTime(r.started_at, now) }}<template v-if="duration(r.started_at, r.finished_at)"> · {{ duration(r.started_at, r.finished_at) }}</template> · {{ fmtUsd(r.cost_usd) }}</span>
        </summary>
        <div class="space-y-2 border-t border-pico-border px-3 py-2">
          <p v-if="r.status === 'running' || r.status === 'queued'" class="text-sm text-zinc-300 motion-safe:animate-pulse" aria-live="polite">Kaisen is working…<template v-if="r.step"> step {{ r.step }}</template></p>
          <p v-if="r.summary" class="whitespace-pre-wrap text-sm text-zinc-200">{{ r.summary }}</p>
          <div v-if="i === 0 && r.status === 'done' && canPick && r.suggestions && r.suggestions.length" class="flex flex-wrap items-center gap-2">
            <span class="meta">Next:</span>
            <button v-for="s in r.suggestions" :key="s" type="button" class="btn-secondary btn-sm text-left" @click="emit('pick', s)">{{ s }}</button>
          </div>
          <div v-if="r.review" class="text-sm">
            <p class="flex flex-wrap items-center gap-2"><span :class="['badge', VERDICT[r.review.verdict]?.cls ?? 'badge-neutral']">{{ VERDICT[r.review.verdict]?.label ?? r.review.verdict }}</span><span v-if="r.review.model" class="meta mono">{{ r.review.model }}</span></p>
            <p v-if="r.review.note" class="help mt-1">{{ r.review.note }}</p>
            <div v-if="r.review.files_changed !== undefined" class="mt-1">
              <p class="help">Reviewer asked for changes; builder answered{{ r.review.files_changed ? '' : ' (no files changed after review)' }}{{ r.review.response && r.review.response.length ? ':' : ' without a response.' }}</p>
              <ul v-if="r.review.response && r.review.response.length" class="mt-1 list-disc space-y-1 pl-5 text-zinc-200"><li v-for="(x, k) in r.review.response" :key="k" class="whitespace-pre-wrap break-words">{{ x }}</li></ul>
            </div>
            <details v-if="r.review.findings && r.review.findings.length" class="mt-1">
              <summary class="cursor-pointer">Findings ({{ r.review.findings.length }})</summary>
              <ul class="mt-1 list-disc space-y-1 pl-5 text-zinc-200"><li v-for="(f, k) in r.review.findings" :key="k" class="whitespace-pre-wrap break-words">{{ f }}</li></ul>
            </details>
          </div>
          <div v-if="r.has_diff">
            <button type="button" class="btn-secondary btn-sm" :aria-expanded="showDiff === r.id" @click="showDiff = showDiff === r.id ? null : r.id">
              <FileDiff :size="14" aria-hidden="true" />{{ showDiff === r.id ? 'Hide changes' : 'Show changes' }}
            </button>
            <BuildDiffPanel v-if="showDiff === r.id" :load="() => getRunDiff(buildId, r.id)" :title="`What run #${r.id} changed`" />
          </div>
          <p v-if="r.error" class="alert-error whitespace-pre-wrap">{{ r.error }}</p>
          <p v-if="r.transcript_trimmed" class="meta">The transcript was removed after 14 days. The summary is kept.</p>
          <details v-else-if="r.transcript && r.transcript.length">
            <summary class="cursor-pointer text-sm">Transcript (redacted)</summary>
            <p class="help">Secrets and tokens are removed before it is shown.</p>
            <div class="mt-1 flex items-start gap-1">
              <pre class="mono max-h-96 min-w-0 flex-1 overflow-auto whitespace-pre-wrap break-words rounded-md bg-pico-bg p-3 text-zinc-200" tabindex="0" :aria-label="`Transcript of run ${r.id}`">{{ text(r) }}</pre>
              <CopyButton :text="text(r)" label="Copy transcript" sm />
            </div>
          </details>
        </div>
      </details>
    </div>
  </section>
</template>
