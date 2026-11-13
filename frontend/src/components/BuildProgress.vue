<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { elapsed, fmtUsd } from '../composables/kaisen'
import type { KaisenRun } from '../types'

// An inline Kaisen message at the end of the thread while a run is active.
const props = defineProps<{ run: KaisenRun; buildUsd: number; capBuild: number; canRun?: boolean; interrupting?: boolean; interruptError?: string }>()
const emit = defineEmits<{ interrupt: [runId: number] }>()
const t = ref(Date.now())
const tick = setInterval(() => (t.value = Date.now()), 1000)
onBeforeUnmount(() => clearInterval(tick))
const headline = computed(() => (props.run.status === 'queued' ? 'Kaisen is getting ready' : 'Kaisen is working'))
const slow = computed(() => props.run.status === 'running' && props.run.step != null && props.run.step > 0)
</script>

<template>
  <div role="group" aria-label="Run progress">
    <!-- iMessage-style: a Kaisen bubble with three animated dots (a static "…" under reduced motion). -->
    <div class="bubble-kaisen inline-flex items-center gap-2 rounded-md px-3 py-2" role="status" aria-live="polite" data-testid="typing">
      <span class="typing-dots" aria-hidden="true"><i /><i /><i /></span>
      <span class="typing-static text-sm text-zinc-300" aria-hidden="true">…</span>
      <span class="sr-only">{{ headline }}</span>
    </div>
    <p class="meta mt-1">{{ slow ? 'Still working. Longer changes can take a few minutes.' : 'This usually takes about a minute.' }}</p>
    <p class="meta mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5">
      <span v-if="run.last_tool" class="mono">{{ run.last_tool }}</span>
      <span v-if="run.context_resets">fresh context ×<span class="mono">{{ run.context_resets }}</span></span>
      <span class="mono">{{ elapsed(run.started_at, t) }}</span>
      <span><span class="mono">{{ fmtUsd(run.cost_usd) }}</span> this run</span>
      <span v-if="capBuild"><span class="mono">{{ fmtUsd(buildUsd) }}</span> of <span class="mono">{{ fmtUsd(capBuild) }}</span> build cap</span>
      <button v-if="canRun && run.status === 'running'" type="button" class="row-act !text-xs" title="Use this if the run looks stuck. Kaisen drops the step in progress and retries from its last saved state." :disabled="interrupting" @click="emit('interrupt', run.id)">{{ interrupting ? 'Interrupting…' : 'Interrupt and retry' }}</button>
    </p>
    <p v-if="interruptError" role="alert" class="alert-error mt-2 whitespace-pre-wrap">{{ interruptError }}</p>
    <p v-for="n in run.notes ?? []" :key="n.at + n.text" class="meta mt-1" role="status">{{ n.text }}</p>
  </div>
</template>
