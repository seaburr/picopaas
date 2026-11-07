<script setup lang="ts">
// Small outlined "Kaisen" tag (same look as the Preview tag) marking an app built with Kaisen. With `to` it links to the build.
defineProps<{ to?: string }>()
const cls = 'inline-block rounded-sm border border-pico-border px-1 text-[11px] font-normal leading-4 text-zinc-400 no-underline'
</script>

<template>
  <RouterLink v-if="to" :to="to" :class="[cls, 'hover:text-white']" title="Built with Kaisen (Preview). Open the build.">Kaisen<span class="sr-only"> (built with Kaisen, Preview)</span></RouterLink>
  <span v-else class="tag" title="Built with Kaisen (Preview)">Kaisen<span class="sr-only"> (built with Kaisen)</span></span>
</template>
