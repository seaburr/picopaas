<script setup lang="ts">
import { computed } from 'vue'
import type { UsageStat } from '../types'

// One table row: label, used, limit, %, thin bar. Render inside a <tbody> of the usage table.
const props = defineProps<{ label: string; usage: UsageStat }>()
const mb = (n: number) => (n / 1024 / 1024).toFixed(n >= 10 * 1024 * 1024 ? 0 : 1)
const GB = 1024 ** 3
const fmt = (n: number) => (props.usage.limit_bytes >= GB ? `${(n / GB).toFixed(2)} GB` : `${mb(n)} MB`)
const pct = computed(() => (props.usage.limit_bytes ? Math.min(100, (props.usage.size_bytes / props.usage.limit_bytes) * 100) : 100))
const warn = computed(() => props.usage.over_quota || pct.value >= 80)
const tone = computed(() => (props.usage.over_quota ? 'text-danger-light' : warn.value ? 'text-amber-400' : ''))
</script>

<template>
  <tr>
    <td>{{ label }}</td>
    <td class="mono text-right" :class="tone">{{ fmt(usage.size_bytes) }}</td>
    <td class="mono text-right">{{ fmt(usage.limit_bytes) }}</td>
    <td class="mono text-right" :class="tone">{{ Math.round(pct) }}%<span v-if="usage.over_quota" class="sr-only"> over cap</span><span v-else-if="warn" class="sr-only"> near cap</span></td>
    <td class="max-md:hidden">
      <div
        class="h-1 overflow-hidden rounded-sm bg-pico-muted"
        role="progressbar"
        :aria-label="`${label} usage`"
        aria-valuemin="0"
        :aria-valuemax="Math.round(usage.limit_bytes / 1024 / 1024)"
        :aria-valuenow="Math.round(usage.size_bytes / 1024 / 1024)"
      >
        <div :class="['h-full', usage.over_quota ? 'bg-danger' : warn ? 'bg-amber-500' : 'bg-accent']" :style="{ width: pct + '%' }" />
      </div>
    </td>
  </tr>
</template>
