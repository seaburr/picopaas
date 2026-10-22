<script setup lang="ts">
// Overview / Edit / Settings nav shared by the three per-app pages. Edit is disabled while the app is suspended.
defineProps<{ id: number | string; active: 'overview' | 'edit' | 'settings'; suspended?: boolean }>()
const base = '-mb-px border-b-2 px-3 py-1.5 text-sm'
const on = 'border-accent-light text-white'
const off = 'border-transparent text-zinc-400 hover:text-white'
</script>

<template>
  <nav class="mb-3 flex gap-1 border-b border-pico-border" aria-label="App sections">
    <RouterLink :to="`/apps/${id}`" :class="[base, active === 'overview' ? on : off]" :aria-current="active === 'overview' ? 'page' : undefined">Overview</RouterLink>
    <span v-if="suspended" :class="[base, 'cursor-not-allowed border-transparent text-zinc-600']" title="Suspended. Contact the administrator to restore.">Edit</span>
    <RouterLink v-else :to="`/apps/${id}/edit`" :class="[base, active === 'edit' ? on : off]" :aria-current="active === 'edit' ? 'page' : undefined">Edit</RouterLink>
    <RouterLink :to="`/apps/${id}/settings`" :class="[base, active === 'settings' ? on : off]" :aria-current="active === 'settings' ? 'page' : undefined">Settings</RouterLink>
  </nav>
</template>
