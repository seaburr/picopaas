<script setup lang="ts">
import CopyButton from './CopyButton.vue'

defineProps<{ hostname: string }>()
</script>

<template>
  <div class="flex items-center gap-1">
    <a :href="`https://${hostname}`" target="_blank" rel="noopener noreferrer" class="link min-w-0 break-all text-sm">
      {{ hostname }}<span class="sr-only"> (opens in new tab)</span>
    </a>
    <CopyButton :text="`https://${hostname}`" label="Copy URL" />
    <a
      :href="`https://${hostname}`"
      target="_blank"
      rel="noopener noreferrer"
      class="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-zinc-400 hover:bg-pico-card hover:text-white"
      title="Open app in a new tab"
      aria-label="Open app in a new tab"
    >
      <svg class="h-4 w-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
        <path d="M9 2h5v5M14 2 7.5 8.5M12 9v4a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h4" />
      </svg>
    </a>
  </div>
</template>
