<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { elapsed, fmtUsd, RUN_KIND_LABEL } from '../composables/kaisen'
import type { KaisenRun } from '../types'

const props = defineProps<{ run: KaisenRun; buildUsd: number; capBuild: number }>()
const t = ref(Date.now())
const tick = setInterval(() => (t.value = Date.now()), 1000)
onBeforeUnmount(() => clearInterval(tick))
const label = computed(() => RUN_KIND_LABEL[props.run.kind] ?? props.run.kind)
</script>

<template>
  <section class="card card-pad" aria-label="Run progress">
    <p class="mono flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-zinc-200">
      <span class="motion-safe:animate-pulse">{{ label }} running</span>
      <span v-if="run.step">step {{ run.step }}</span>
      <span v-if="run.last_tool" class="mono">{{ run.last_tool }}</span>
      <span>{{ elapsed(run.started_at, t) }}</span>
      <span>{{ fmtUsd(run.cost_usd) }} this run</span>
      <span class="meta">{{ fmtUsd(buildUsd) }} of {{ fmtUsd(capBuild) }} build cap</span>
    </p>
  </section>
</template>
