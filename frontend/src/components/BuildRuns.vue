<script setup lang="ts">
import { ChevronRight } from 'lucide-vue-next'
import CopyButton from './CopyButton.vue'
import StatusBadge from './StatusBadge.vue'
import { relTime } from '../composables/appUi'
import { duration, fmtUsd, RUN_KIND_LABEL } from '../composables/kaisen'
import type { KaisenRun } from '../types'

defineProps<{ runs: KaisenRun[]; now: number }>()

// Plain text only: prefix each turn with its role; never rendered as HTML.
const text = (r: KaisenRun) => (r.transcript ?? []).map((t) => `${t.role}: ${t.text}`).join('\n\n')
</script>

<template>
  <section class="card card-pad" aria-labelledby="h-runs">
    <h2 id="h-runs" class="section-title mb-2">Runs ({{ runs.length }})</h2>
    <p v-if="runs.length === 0" class="text-sm text-zinc-300">No runs yet.</p>
    <div v-else class="space-y-2">
      <details v-for="(r, i) in runs" :key="r.id" class="group rounded-md border border-pico-border" :open="i === 0 && (r.status === 'running' || r.status === 'queued' || r.status === 'error' || r.status === 'stopped')">
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
