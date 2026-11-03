<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { ArrowRight } from 'lucide-vue-next'
import PreviewBadge from './PreviewBadge.vue'

// New-app chooser: image, template and (when enabled) Kaisen as equal options.
const props = defineProps<{ kaisen?: boolean; current?: 'image' | 'template' }>()
const isTemplate = computed(() => props.current === 'template')
</script>

<template>
  <div class="mb-4 grid grid-cols-1 gap-2 sm:grid-cols-2" :class="kaisen ? 'lg:grid-cols-3' : ''" role="group" aria-label="How do you want to create this app?">
    <component :is="isTemplate ? RouterLink : 'div'" v-bind="isTemplate ? { to: '/apps/new' } : { 'aria-current': 'true' }" class="card card-pad block" :class="isTemplate ? 'hover:bg-pico-card' : 'border-accent-light'">
      <h2 class="section-title">Deploy an image</h2>
      <p class="mt-1 text-sm text-zinc-300">Run a public Docker image you already have. Live in about a minute.</p>
      <span v-if="!isTemplate" class="badge-info mt-2">Selected</span>
      <span v-else class="link mt-2 inline-flex items-center gap-1">Start <ArrowRight :size="14" aria-hidden="true" /></span>
    </component>
    <component :is="isTemplate ? 'div' : RouterLink" v-bind="isTemplate ? { 'aria-current': 'true' } : { to: '/templates' }" class="card card-pad block" :class="isTemplate ? 'border-accent-light' : 'hover:bg-pico-card'">
      <h2 class="section-title">Deploy from a template</h2>
      <p class="mt-1 text-sm text-zinc-300">Start from a ready-made app such as Ghost, Grafana or n8n.</p>
      <span v-if="isTemplate" class="badge-info mt-2">Selected</span>
      <span v-else class="link mt-2 inline-flex items-center gap-1">Browse <ArrowRight :size="14" aria-hidden="true" /></span>
    </component>
    <RouterLink v-if="kaisen" to="/kaisen/new" class="card card-pad block hover:bg-pico-card">
      <h2 class="section-title">Build with Kaisen <PreviewBadge /></h2>
      <p class="mt-1 text-sm text-zinc-300">Describe an app in plain words. Kaisen writes it, runs it and deploys it here.</p>
      <span class="link mt-2 inline-flex items-center gap-1">Start <ArrowRight :size="14" aria-hidden="true" /></span>
    </RouterLink>
  </div>
</template>
