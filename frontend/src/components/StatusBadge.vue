<script setup lang="ts">
defineProps<{ status: string }>()

const colorFor: Record<string, string> = {
  pending: 'bg-status-pending/15 text-status-pending border-status-pending/30',
  deploying: 'bg-status-pending/15 text-status-pending border-status-pending/30',
  running: 'bg-status-running/15 text-status-running border-status-running/30',
  error: 'bg-status-error/15 text-status-error border-status-error/30',
  stopped: 'bg-status-stopped/15 text-status-stopped border-status-stopped/30',
}
</script>

<template>
  <span
    :class="[
      'inline-block rounded-full border px-2 py-0.5 text-xs font-medium capitalize',
      colorFor[status] ?? 'bg-pico-muted/15 text-zinc-300 border-pico-muted/30',
    ]"
  >
    {{ status }}
  </span>
</template>
