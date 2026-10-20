<script setup lang="ts">
import { computed } from 'vue'

// Hand-drawn SVG polyline (no chart library: CSP + bundle size). y axis is 0..max(limit, peak).
const props = defineProps<{ values: number[]; limit: number; label: string; unit: string }>()
const W = 160
const H = 36

const top = computed(() => Math.max(props.limit, ...props.values, 1))
const current = computed(() => (props.values.length ? props.values[props.values.length - 1] : 0))
const near = computed(() => props.limit > 0 && current.value >= props.limit * 0.8)
const points = computed(() => {
  const n = props.values.length
  return props.values
    .map((v, i) => `${n === 1 ? W : (i / (n - 1)) * W},${H - 1 - (v / top.value) * (H - 2)}`)
    .join(' ')
})
</script>

<template>
  <div>
    <div class="flex items-baseline justify-between text-sm">
      <span class="meta">{{ label }}</span>
      <span :class="near ? 'text-amber-400' : 'text-zinc-200'">{{ current }}{{ unit }} / {{ limit }}{{ unit }}</span>
    </div>
    <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" class="mt-1 h-9 w-full" role="img" :aria-label="`${label} usage over the last few hours`">
      <line x1="0" :x2="W" :y1="H - 1 - (limit / top) * (H - 2)" :y2="H - 1 - (limit / top) * (H - 2)" stroke="currentColor" class="text-zinc-700" stroke-dasharray="3 3" stroke-width="1" vector-effect="non-scaling-stroke" />
      <polyline :points="points" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" vector-effect="non-scaling-stroke" :class="near ? 'text-amber-400' : 'text-emerald-400'" />
    </svg>
  </div>
</template>
