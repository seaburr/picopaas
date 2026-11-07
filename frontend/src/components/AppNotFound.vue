<script setup lang="ts">
import { useProjectsStore } from '../stores/projects'

// Shared "app not found / not yours" panel for the health and settings pages.
const projects = useProjectsStore()
</script>

<template>
  <div class="sec sec-pad">
    <p class="section-title">App not found</p>
    <p class="help">It may have been deleted.</p>
    <RouterLink :to="projects.appsHome" class="btn-secondary btn-sm mt-3">Back to apps</RouterLink>
  </div>
</template>
