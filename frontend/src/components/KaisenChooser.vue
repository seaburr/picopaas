<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { projectKaisen, projectNewApp } from '../utils/paths'
import PreviewBadge from './PreviewBadge.vue'

// First step of "New app": three equal options (Kaisen only when enabled for the user).
const props = defineProps<{ kaisen?: boolean; projectId?: number | null }>()
</script>

<template>
  <div class="mb-4 grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2" :class="kaisen ? 'lg:grid-cols-3' : ''" role="group" aria-label="How do you want to create this app?">
    <RouterLink :to="{ path: props.projectId ? projectNewApp(props.projectId) : '/apps/new', query: { from: 'image' } }" class="sec block pt-3 hover:bg-pico-card">
      <h2 class="section-title">Deploy an image</h2>
      <p class="mt-1 text-sm text-zinc-300">Run a public Docker image you already have. Live in about a minute.</p>
      <span class="link mt-2 inline-block">Start</span>
    </RouterLink>
    <RouterLink :to="props.projectId ? { path: '/templates', query: { project: props.projectId } } : '/templates'" class="sec block pt-3 hover:bg-pico-card">
      <h2 class="section-title">Deploy from a template</h2>
      <p class="mt-1 text-sm text-zinc-300">Start from a ready-made app such as Ghost, Grafana or n8n.</p>
      <span class="link mt-2 inline-block">Browse</span>
    </RouterLink>
    <RouterLink v-if="kaisen" :to="props.projectId ? projectKaisen(props.projectId, '/new') : '/kaisen/new'" class="sec block pt-3 hover:bg-pico-card">
      <h2 class="section-title">Build with Kaisen <PreviewBadge /></h2>
      <p class="mt-1 text-sm text-zinc-300">Describe an app in plain words. Kaisen writes it, runs it and deploys it here.</p>
      <span class="link mt-2 inline-block">Start</span>
    </RouterLink>
  </div>
</template>
