<script setup lang="ts">
import { useProjectsStore } from '../stores/projects'

// Label-only project select for the Apps and Kaisen headers. Hidden until the user has a shared project.
defineProps<{ label: string }>()
const projects = useProjectsStore()

function onPick(e: Event) {
  const id = Number((e.target as HTMLSelectElement).value)
  if (id) projects.select(id)
}
</script>

<template>
  <span v-if="projects.hasShared" class="inline-flex items-center gap-2 text-sm text-zinc-300">
    {{ label }}
    <select class="input w-auto max-w-56 min-w-0" aria-label="Project" :value="projects.current?.id" @change="onPick">
      <option v-for="p in projects.projects" :key="p.id" :value="p.id">{{ p.is_personal ? 'Personal' : p.name }}</option>
    </select>
  </span>
</template>
