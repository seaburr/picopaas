<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { ArrowRight } from 'lucide-vue-next'
import PreviewBadge from './PreviewBadge.vue'

// First step of "New app": three equal options (Kaisen only when enabled for the user).
defineProps<{ kaisen?: boolean }>()
</script>

<template>
  <div class="mb-4 grid grid-cols-1 gap-2 sm:grid-cols-2" :class="kaisen ? 'lg:grid-cols-3' : ''" role="group" aria-label="How do you want to create this app?">
    <RouterLink :to="{ path: '/apps/new', query: { from: 'image' } }" class="card card-pad block hover:bg-pico-card">
      <h2 class="section-title">Deploy an image</h2>
      <p class="mt-1 text-sm text-zinc-300">Run a public Docker image you already have. Live in about a minute.</p>
      <span class="link mt-2 inline-flex items-center gap-1">Start <ArrowRight :size="14" aria-hidden="true" /></span>
    </RouterLink>
    <RouterLink to="/templates" class="card card-pad block hover:bg-pico-card">
      <h2 class="section-title">Deploy from a template</h2>
      <p class="mt-1 text-sm text-zinc-300">Start from a ready-made app such as Ghost, Grafana or n8n.</p>
      <span class="link mt-2 inline-flex items-center gap-1">Browse <ArrowRight :size="14" aria-hidden="true" /></span>
    </RouterLink>
    <RouterLink v-if="kaisen" to="/kaisen/new" class="card card-pad block hover:bg-pico-card">
      <h2 class="section-title">Build with Kaisen <PreviewBadge /></h2>
      <p class="mt-1 text-sm text-zinc-300">Describe an app in plain words. Kaisen writes it, runs it and deploys it here.</p>
      <span class="link mt-2 inline-flex items-center gap-1">Start <ArrowRight :size="14" aria-hidden="true" /></span>
    </RouterLink>
  </div>
</template>
