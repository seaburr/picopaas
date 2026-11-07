<script setup lang="ts">
import { computed } from 'vue'

// Hand-drawn SVG polyline (no chart library: CSP + bundle size). y axis is 0..max(limit, peak). Graph only: the
// numbers (used / limit / %) live in the table cells next to it.
const props = defineProps<{ values: number[]; limit: number; label: string; unit: string; level?: 'ok' | 'warn' | 'err' }>()
const W = 160
const H = 44

const top = computed(() => Math.max(props.limit, ...props.values, 1))
const current = computed(() => (props.values.length ? props.values[props.values.length - 1] : 0))
const points = computed(() => {
  const n = props.values.length
  return props.values
    .map((v, i) => `${n === 1 ? W : (i / (n - 1)) * W},${H - 2 - (v / top.value) * (H - 4)}`)
    .join(' ')
})
</script>

<template>
  <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" class="block h-11 w-full" role="img" :aria-label="`${label} usage over the last few hours, now ${current}${unit} of ${limit}${unit}`">
    <line x1="0" :x2="W" :y1="H - 0.5" :y2="H - 0.5" stroke="currentColor" class="text-zinc-600" stroke-width="1.5" vector-effect="non-scaling-stroke" />
    <line x1="0" :x2="W" :y1="H - 1 - (limit / top) * (H - 2)" :y2="H - 1 - (limit / top) * (H - 2)" stroke="currentColor" class="text-zinc-600" stroke-dasharray="3 3" stroke-width="1.5" vector-effect="non-scaling-stroke" />
    <polyline :points="points" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linejoin="round" vector-effect="non-scaling-stroke" :style="{ color: `var(--${level ?? 'ok'})` }" />
  </svg>
</template>
