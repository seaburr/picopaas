<script setup lang="ts">
import { computed } from 'vue'

// One resource as a gauge row: label, "used / limit unit" and percent in mono, a 10px bar coloured by state
// (ok < 75%, warn >= 75%, err >= 90% or over its cap).
// Limits and values come from the API; this only formats them.
const props = defineProps<{
  label: string
  used: string // already formatted, no unit ("212")
  limit: string // ("384")
  unit: string // ("MB")
  pct: number // 0..100+
  over?: boolean
}>()
const WARN = 75
const ERR = 90
const level = computed<'ok' | 'warn' | 'err'>(() => (props.over || props.pct >= ERR ? 'err' : props.pct >= WARN ? 'warn' : 'ok'))
const width = computed(() => Math.max(0, Math.min(100, props.pct)))
const word = computed(() => (props.over ? ' over cap' : level.value === 'err' ? ' critical' : level.value === 'warn' ? ' high' : ''))
</script>

<template>
  <li class="min-w-0 border-b border-pico-border py-2.5 first:pt-1 last:border-b-0">
    <div class="flex flex-wrap items-baseline justify-between gap-x-3">
      <span class="text-sm text-zinc-200">{{ label }}</span>
      <span class="mono text-sm">
        <span :style="level === 'ok' ? undefined : { color: `var(--${level === 'err' ? 'err' : 'warn'})` }">{{ used }}</span>
        <span class="text-zinc-400"> / {{ limit }} {{ unit }} · </span>
        <span :style="level === 'ok' ? undefined : { color: `var(--${level === 'err' ? 'err' : 'warn'})` }">{{ Math.round(pct) }}%</span>
        <span class="sr-only">{{ word }}</span>
      </span>
    </div>
    <div
      class="mt-1.5 h-2.5 overflow-hidden rounded-sm bg-pico-muted"
      role="progressbar"
      :aria-label="`${label} usage`"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-valuenow="Math.round(width)"
    >
      <div class="h-full" :style="{ width: width + '%', background: `var(--${level})` }" />
    </div>
  </li>
</template>
