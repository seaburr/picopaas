<script setup lang="ts">
import { ExternalLink } from 'lucide-vue-next'
import CopyButton from './CopyButton.vue'
import IconButton from './IconButton.vue'

defineProps<{ hostname: string }>()
</script>

<template>
  <div class="flex min-w-0 items-center gap-0.5">
    <a :href="`https://${hostname}`" target="_blank" rel="noopener noreferrer" class="link mono relative truncate" :title="hostname">
      {{ hostname }}<span class="sr-only"> (opens in new tab)</span>
    </a>
    <CopyButton :text="`https://${hostname}`" label="Copy URL" sm />
    <IconButton :icon="ExternalLink" label="Open app in a new tab" :href="`https://${hostname}`" sm />
  </div>
</template>
