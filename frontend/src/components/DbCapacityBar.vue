<script setup lang="ts">
import type { DbServer } from '../types'
defineProps<{ server: DbServer }>()
</script>

<template>
  <div class="flex items-center gap-2">
    <div class="h-1.5 w-24 shrink-0 overflow-hidden rounded bg-pico-card">
      <div
        :class="['h-full', server.warn ? 'bg-amber-500' : 'bg-accent']"
        :style="{ width: Math.min(100, server.max_databases ? (server.used / server.max_databases) * 100 : 100) + '%' }"
      />
    </div>
    <span class="mono" :class="server.warn ? 'text-amber-400' : 'text-zinc-400'">
      {{ server.used }}/{{ server.max_databases }}
    </span>
  </div>
</template>
