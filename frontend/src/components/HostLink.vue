<script setup lang="ts">
import CopyButton from './CopyButton.vue'

// compact: list rows. One truncated line, no Copy (Copy lives on App health).
defineProps<{ hostname: string; compact?: boolean }>()
</script>

<template>
  <div class="flex min-w-0 items-center gap-x-2.5" :class="compact ? '' : 'flex-wrap'">
    <a :href="`https://${hostname}`" target="_blank" rel="noopener noreferrer" class="link mono min-w-0" :class="compact ? 'truncate' : 'break-all'" :title="hostname">
      {{ hostname }}<span class="sr-only"> (opens in new tab)</span>
    </a>
    <CopyButton v-if="!compact" class="shrink-0" :text="`https://${hostname}`" label="Copy URL" />
  </div>
</template>
