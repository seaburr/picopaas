<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { elapsed, fmtUsd, RUN_KIND_LABEL } from '../composables/kaisen'
import type { KaisenRun } from '../types'

const props = defineProps<{ run: KaisenRun; buildUsd: number; capBuild: number; canRun?: boolean; interrupting?: boolean; interruptError?: string }>()
const emit = defineEmits<{ interrupt: [runId: number] }>()
const t = ref(Date.now())
const tick = setInterval(() => (t.value = Date.now()), 1000)
onBeforeUnmount(() => clearInterval(tick))
const label = computed(() => RUN_KIND_LABEL[props.run.kind] ?? props.run.kind)
</script>

<template>
  <section class="card card-pad" aria-label="Run progress">
    <p class="mono flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-zinc-200">
      <span class="motion-safe:animate-pulse">{{ label }} {{ run.status === 'queued' ? 'queued, starting again' : 'running' }}</span>
      <span v-if="run.step">step {{ run.step }}</span>
      <span v-if="run.last_tool" class="mono">{{ run.last_tool }}</span>
      <span v-if="run.context_resets">fresh context ×{{ run.context_resets }}</span>
      <span>{{ elapsed(run.started_at, t) }}</span>
      <span>{{ fmtUsd(run.cost_usd) }} this run</span>
      <span class="meta">{{ fmtUsd(buildUsd) }} of {{ fmtUsd(capBuild) }} build cap</span>
      <button v-if="canRun && run.status === 'running'" type="button" class="btn-secondary btn-sm font-sans sm:ml-auto" title="Use this if the run looks stuck. Kaisen drops the step in progress and retries from its last saved state." :disabled="interrupting" @click="emit('interrupt', run.id)">{{ interrupting ? 'Interrupting…' : 'Interrupt and retry' }}</button>
    </p>
    <p v-if="interruptError" role="alert" class="alert-error mt-2 whitespace-pre-wrap">{{ interruptError }}</p>
    <p v-for="n in run.notes ?? []" :key="n.at + n.text" class="meta mt-2" role="status">{{ n.text }}</p>
  </section>
</template>
