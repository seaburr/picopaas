<script setup lang="ts">
import { ExternalLink } from 'lucide-vue-next'
import CopyButton from './CopyButton.vue'
import IconButton from './IconButton.vue'

defineProps<{ hostname: string }>()
</script>

<template>
  <div class="flex min-w-0 items-center gap-0.5">
    <a :href="`https://${hostname}`" target="_blank" rel="noopener noreferrer" class="link mono relative min-w-0 truncate" :title="hostname">
      {{ hostname }}<span class="sr-only"> (opens in new tab)</span>
    </a>
    <CopyButton class="shrink-0" :text="`https://${hostname}`" label="Copy URL" sm />
    <IconButton class="shrink-0" :icon="ExternalLink" label="Open app in a new tab" :href="`https://${hostname}`" sm />
  </div>
</template>
