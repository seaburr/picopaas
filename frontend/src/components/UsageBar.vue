<script setup lang="ts">
import { computed } from 'vue'
import type { UsageStat } from '../types'

const props = defineProps<{ label: string; usage: UsageStat }>()
const mb = (n: number) => (n / 1024 / 1024).toFixed(n >= 10 * 1024 * 1024 ? 0 : 1)
const GB = 1024 ** 3
const fmt = (n: number) => (props.usage.limit_bytes >= GB ? `${(n / GB).toFixed(2)} GB` : `${mb(n)} MB`)
const pct = computed(() => (props.usage.limit_bytes ? Math.min(100, (props.usage.size_bytes / props.usage.limit_bytes) * 100) : 100))
const warn = computed(() => props.usage.over_quota || pct.value >= 80)
</script>

<template>
  <div>
    <div class="flex items-baseline justify-between gap-2 text-sm">
      <span class="meta">{{ label }}</span>
      <span class="mono" :class="usage.over_quota ? 'text-danger-light' : warn ? 'text-amber-400' : 'text-zinc-300'">
        {{ fmt(usage.size_bytes) }} / {{ fmt(usage.limit_bytes) }}<span v-if="usage.over_quota"> · over cap</span><span v-else-if="warn"> · near cap</span>
      </span>
    </div>
    <div
      class="mt-1 h-1.5 overflow-hidden rounded-sm bg-pico-card"
      role="progressbar"
      :aria-label="`${label} usage`"
      aria-valuemin="0"
      :aria-valuemax="Math.round(usage.limit_bytes / 1024 / 1024)"
      :aria-valuenow="Math.round(usage.size_bytes / 1024 / 1024)"
    >
      <div :class="['h-full', usage.over_quota ? 'bg-danger' : warn ? 'bg-amber-500' : 'bg-accent']" :style="{ width: pct + '%' }" />
    </div>
  </div>
</template>
