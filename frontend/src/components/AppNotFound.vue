<script setup lang="ts">
import { computed } from 'vue'
import { useProjectsStore } from '../stores/projects'
import { projectKaisen } from '../utils/paths'

// Shared not-found panel for pages that load one thing by id (app health/settings, project, Kaisen build).
const props = withDefaults(defineProps<{ kind?: 'app' | 'project' | 'build' }>(), { kind: 'app' })
const projects = useProjectsStore()
const title = computed(() => ({ app: 'App not found', project: 'Project not found', build: 'Build not found' })[props.kind])
const kaisenHome = computed(() => (projects.current ? projectKaisen(projects.current.id) : '/kaisen'))
</script>

<template>
  <div class="max-w-md py-4" data-testid="not-found">
    <h1 class="page-title">{{ title }}</h1>
    <p class="mt-1 text-sm text-zinc-300">It doesn't exist, or your account can't see it.</p>
    <div class="mt-4 flex flex-wrap gap-2">
      <RouterLink v-if="kind === 'build'" :to="kaisenHome" class="btn-secondary btn-sm">Kaisen</RouterLink>
      <RouterLink :to="projects.appsHome" class="btn-secondary btn-sm">Apps</RouterLink>
      <RouterLink to="/projects" class="btn-secondary btn-sm">Projects</RouterLink>
    </div>
  </div>
</template>
