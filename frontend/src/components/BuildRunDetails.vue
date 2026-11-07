<script setup lang="ts">
import { ref } from 'vue'
import BuildDiffPanel from './BuildDiffPanel.vue'
import { getRunDiff } from '../api/kaisen'
import CopyButton from './CopyButton.vue'
import { RUN_KIND_LABEL } from '../composables/kaisen'
import type { KaisenRun } from '../types'

defineProps<{ buildId: number; run: KaisenRun }>()
const showDiff = ref<number | null>(null)

// Plain text only: prefix each turn with its role; never rendered as HTML.
const VERDICT: Record<string, { label: string; cls: string }> = {
  approve: { label: 'Approved', cls: 'ok' },
  changes: { label: 'Changes requested', cls: 'warn' },
  skipped: { label: 'Review skipped', cls: 'off' },
}
const PKG_VERDICT: Record<string, { label: string; cls: string }> = {
  current: { label: 'Packages current', cls: 'ok' },
  outdated: { label: 'Packages outdated', cls: 'warn' },
  skipped: { label: 'Package check skipped', cls: 'off' },
}
const isReviewer = (t: { role: string; text: string }) => t.role === 'user' && t.text.startsWith('[reviewer]')
const body = (t: { role: string; text: string }) => (isReviewer(t) ? t.text.replace(/^\[reviewer\]\s*/, '') : t.text)
const label = (t: { role: string; text: string }) => (isReviewer(t) ? 'Reviewer' : t.role)
const text = (r: KaisenRun) => (r.transcript ?? []).map((t) => `${label(t)}: ${body(t)}`).join('\n\n')
</script>

<template>
  <div class="space-y-2 text-sm">
    <p class="meta"><span class="mono">Run #{{ run.id }}</span> · {{ RUN_KIND_LABEL[run.kind] ?? run.kind }}</p>
    <div v-if="run.review">
      <p class="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span class="meta">Reviewer</span>
        <span :class="['st', `tone-${(VERDICT[run.review.verdict]?.cls) ?? 'off'}`]"><span :class="['dot', `dot-${(VERDICT[run.review.verdict]?.cls) ?? 'off'}`]" aria-hidden="true" />{{ VERDICT[run.review.verdict]?.label ?? run.review.verdict }}</span>
        <span v-if="run.review.model" class="meta mono">{{ run.review.model }}</span>
      </p>
      <p v-if="run.review.note" class="help">{{ run.review.note }}</p>
      <div v-if="run.review.files_changed !== undefined" class="mt-1">
        <p class="help">Reviewer asked for changes; builder answered{{ run.review.files_changed ? '' : ' (no files changed after review)' }}{{ run.review.response && run.review.response.length ? ':' : ' without a response.' }}</p>
        <ul v-if="run.review.response && run.review.response.length" class="mt-1 list-disc space-y-1 pl-5 text-zinc-200"><li v-for="(x, k) in run.review.response" :key="k" class="whitespace-pre-wrap [overflow-wrap:anywhere]">{{ x }}</li></ul>
      </div>
      <details v-if="run.review.findings && run.review.findings.length" class="disc mt-1">
        <summary>Findings ({{ run.review.findings.length }})</summary>
        <ul class="mt-1 list-disc space-y-1 pl-5 text-zinc-200"><li v-for="(f, k) in run.review.findings" :key="k" class="whitespace-pre-wrap [overflow-wrap:anywhere]">{{ f }}</li></ul>
      </details>
    </div>
    <div v-if="run.packages">
      <p class="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span class="meta">Package steward</span>
        <span :class="['st', `tone-${(PKG_VERDICT[run.packages.verdict]?.cls) ?? 'off'}`]"><span :class="['dot', `dot-${(PKG_VERDICT[run.packages.verdict]?.cls) ?? 'off'}`]" aria-hidden="true" />{{ PKG_VERDICT[run.packages.verdict]?.label ?? run.packages.verdict }}</span>
        <span v-if="run.packages.model" class="meta mono">{{ run.packages.model }}</span>
        <span v-if="run.packages.verdict !== 'skipped'" class="meta"><span class="mono">{{ run.packages.checked }}</span> {{ run.packages.checked === 1 ? 'package' : 'packages' }} checked</span>
      </p>
      <p v-if="run.packages.note" class="help">{{ run.packages.note }}</p>
      <div v-if="run.packages.files_changed !== undefined" class="mt-1">
        <p class="help">Steward asked for upgrades; builder answered{{ run.packages.files_changed ? '' : ' (no files changed after the check)' }}{{ run.packages.response && run.packages.response.length ? ':' : ' without a response.' }}</p>
        <ul v-if="run.packages.response && run.packages.response.length" class="mt-1 list-disc space-y-1 pl-5 text-zinc-200"><li v-for="(x, k) in run.packages.response" :key="k" class="whitespace-pre-wrap [overflow-wrap:anywhere]">{{ x }}</li></ul>
      </div>
      <div v-if="run.packages.requested && run.packages.requested.length" class="mt-1">
        <p class="help">Kept as your request names</p>
        <ul class="mt-1 list-disc space-y-1 pl-5 text-zinc-200"><li v-for="(x, k) in run.packages.requested" :key="k" class="whitespace-pre-wrap [overflow-wrap:anywhere]">{{ x }}</li></ul>
      </div>
      <details v-if="run.packages.findings && run.packages.findings.length" class="disc mt-1">
        <summary>Findings ({{ run.packages.findings.length }})</summary>
        <ul class="mt-1 list-disc space-y-1 pl-5 text-zinc-200"><li v-for="(f, k) in run.packages.findings" :key="k" class="whitespace-pre-wrap [overflow-wrap:anywhere]">{{ f }}</li></ul>
      </details>
    </div>
    <div v-if="run.has_diff">
      <button type="button" class="btn-secondary btn-sm" :aria-expanded="showDiff === run.id" @click="showDiff = showDiff === run.id ? null : run.id">{{ showDiff === run.id ? 'Hide changes' : 'Show changes' }}</button>
      <BuildDiffPanel v-if="showDiff === run.id" :load="() => getRunDiff(buildId, run.id)" :title="`What run #${run.id} changed`" />
    </div>
    <p v-if="run.transcript_trimmed" class="meta">The transcript was removed after 14 days. The summary is kept.</p>
    <details v-else-if="run.transcript && run.transcript.length" class="disc">
      <summary>Transcript (redacted)</summary>
      <p class="help">Secrets and tokens are removed before it is shown.</p>
      <div class="mt-1 flex items-start gap-1">
        <pre class="mono max-h-96 min-w-0 flex-1 overflow-auto whitespace-pre-wrap rounded-md bg-pico-bg p-3 text-zinc-200 [overflow-wrap:anywhere]" tabindex="0" :aria-label="`Transcript of run ${run.id}`">{{ text(run) }}</pre>
        <CopyButton :text="text(run)" label="Copy transcript" sm />
      </div>
    </details>
  </div>
</template>
