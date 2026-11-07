<script setup lang="ts">
import { ref } from 'vue'
import { FileDiff } from 'lucide-vue-next'
import BuildDiffPanel from './BuildDiffPanel.vue'
import { getRunDiff } from '../api/kaisen'
import CopyButton from './CopyButton.vue'
import type { KaisenRun } from '../types'

defineProps<{ buildId: number; run: KaisenRun; isLatest: boolean; canPick?: boolean; canResume?: boolean; resuming?: boolean }>()
const showDiff = ref<number | null>(null)
const emit = defineEmits<{ pick: [text: string]; resume: [] }>()

// Plain text only: prefix each turn with its role; never rendered as HTML.
const VERDICT: Record<string, { label: string; cls: string }> = {
  approve: { label: 'Approved', cls: 'badge-ok' },
  changes: { label: 'Changes requested', cls: 'badge-warn' },
  skipped: { label: 'Review skipped', cls: 'badge-neutral' },
}
const PKG_VERDICT: Record<string, { label: string; cls: string }> = {
  current: { label: 'Packages current', cls: 'badge-ok' },
  outdated: { label: 'Packages outdated', cls: 'badge-warn' },
  skipped: { label: 'Package check skipped', cls: 'badge-neutral' },
}
const isReviewer = (t: { role: string; text: string }) => t.role === 'user' && t.text.startsWith('[reviewer]')
const body = (t: { role: string; text: string }) => (isReviewer(t) ? t.text.replace(/^\[reviewer\]\s*/, '') : t.text)
const label = (t: { role: string; text: string }) => (isReviewer(t) ? 'Reviewer' : t.role)
const text = (r: KaisenRun) => (r.transcript ?? []).map((t) => `${label(t)}: ${body(t)}`).join('\n\n')
</script>

<template>
<div class="space-y-2">
          <p v-if="run.status === 'running' || run.status === 'queued'" class="text-sm text-zinc-300 motion-safe:animate-pulse" aria-live="polite">Kaisen is working…<template v-if="run.step"> step {{ run.step }}</template></p>
          <p v-if="run.summary" class="whitespace-pre-wrap text-sm text-zinc-200">{{ run.summary }}</p>
          <div v-if="isLatest && run.status === 'done' && canPick && run.suggestions && run.suggestions.length" class="flex flex-wrap items-center gap-2">
            <span class="meta">Next:</span>
            <button v-for="s in run.suggestions" :key="s" type="button" class="btn-secondary btn-sm text-left" @click="emit('pick', s)">{{ s }}</button>
          </div>
          <div v-if="run.review" class="text-sm">
            <p class="flex flex-wrap items-center gap-2"><span class="meta">Reviewer</span><span :class="['badge', VERDICT[run.review.verdict]?.cls ?? 'badge-neutral']">{{ VERDICT[run.review.verdict]?.label ?? run.review.verdict }}</span><span v-if="run.review.model" class="meta mono">{{ run.review.model }}</span></p>
            <p v-if="run.review.note" class="help mt-1">{{ run.review.note }}</p>
            <div v-if="run.review.files_changed !== undefined" class="mt-1">
              <p class="help">Reviewer asked for changes; builder answered{{ run.review.files_changed ? '' : ' (no files changed after review)' }}{{ run.review.response && run.review.response.length ? ':' : ' without a response.' }}</p>
              <ul v-if="run.review.response && run.review.response.length" class="mt-1 list-disc space-y-1 pl-5 text-zinc-200"><li v-for="(x, k) in run.review.response" :key="k" class="whitespace-pre-wrap break-words">{{ x }}</li></ul>
            </div>
            <details v-if="run.review.findings && run.review.findings.length" class="mt-1">
              <summary class="cursor-pointer">Findings ({{ run.review.findings.length }})</summary>
              <ul class="mt-1 list-disc space-y-1 pl-5 text-zinc-200"><li v-for="(f, k) in run.review.findings" :key="k" class="whitespace-pre-wrap break-words">{{ f }}</li></ul>
            </details>
          </div>
          <div v-if="run.packages" class="text-sm">
            <p class="flex flex-wrap items-center gap-2">
              <span class="meta">Package steward</span>
              <span :class="['badge', PKG_VERDICT[run.packages.verdict]?.cls ?? 'badge-neutral']">{{ PKG_VERDICT[run.packages.verdict]?.label ?? run.packages.verdict }}</span>
              <span v-if="run.packages.model" class="meta mono">{{ run.packages.model }}</span>
              <span v-if="run.packages.verdict !== 'skipped'" class="meta">{{ run.packages.checked }} {{ run.packages.checked === 1 ? 'package' : 'packages' }} checked</span>
            </p>
            <p v-if="run.packages.note" class="help mt-1">{{ run.packages.note }}</p>
            <div v-if="run.packages.files_changed !== undefined" class="mt-1">
              <p class="help">Steward asked for upgrades; builder answered{{ run.packages.files_changed ? '' : ' (no files changed after the check)' }}{{ run.packages.response && run.packages.response.length ? ':' : ' without a response.' }}</p>
              <ul v-if="run.packages.response && run.packages.response.length" class="mt-1 list-disc space-y-1 pl-5 text-zinc-200"><li v-for="(x, k) in run.packages.response" :key="k" class="whitespace-pre-wrap break-words">{{ x }}</li></ul>
            </div>
            <div v-if="run.packages.requested && run.packages.requested.length" class="mt-1">
              <p class="help">Kept as your request names</p>
              <ul class="mt-1 list-disc space-y-1 pl-5 text-zinc-200"><li v-for="(x, k) in run.packages.requested" :key="k" class="whitespace-pre-wrap break-words">{{ x }}</li></ul>
            </div>
            <details v-if="run.packages.findings && run.packages.findings.length" class="mt-1">
              <summary class="cursor-pointer">Findings ({{ run.packages.findings.length }})</summary>
              <ul class="mt-1 list-disc space-y-1 pl-5 text-zinc-200"><li v-for="(f, k) in run.packages.findings" :key="k" class="whitespace-pre-wrap break-words">{{ f }}</li></ul>
            </details>
          </div>
          <div v-if="run.has_diff">
            <button type="button" class="btn-secondary btn-sm" :aria-expanded="showDiff === run.id" @click="showDiff = showDiff === run.id ? null : run.id">
              <FileDiff :size="14" aria-hidden="true" />{{ showDiff === run.id ? 'Hide changes' : 'Show changes' }}
            </button>
            <BuildDiffPanel v-if="showDiff === run.id" :load="() => getRunDiff(buildId, run.id)" :title="`What run #${run.id} changed`" />
          </div>
          <div v-if="run.error" class="alert-error flex flex-wrap items-center justify-between gap-2">
            <p class="whitespace-pre-wrap">{{ run.error }}</p>
            <button v-if="isLatest && canResume && (run.status === 'error' || run.status === 'stopped')" type="button" class="btn-secondary btn-sm" :disabled="resuming" @click="emit('resume')">{{ resuming ? 'Resuming…' : 'Resume' }}</button>
          </div>
          <p v-if="run.transcript_trimmed" class="meta">The transcript was removed after 14 days. The summary is kept.</p>
          <details v-else-if="run.transcript && run.transcript.length">
            <summary class="cursor-pointer text-sm">Transcript (redacted)</summary>
            <p class="help">Secrets and tokens are removed before it is shown.</p>
            <div class="mt-1 flex items-start gap-1">
              <pre class="mono max-h-96 min-w-0 flex-1 overflow-auto whitespace-pre-wrap break-words rounded-md bg-pico-bg p-3 text-zinc-200" tabindex="0" :aria-label="`Transcript of run ${run.id}`">{{ text(run) }}</pre>
              <CopyButton :text="text(run)" label="Copy transcript" sm />
            </div>
          </details>
</div>
</template>
