<script setup lang="ts">
// Shared "app not found / not yours" panel for the health and settings pages.
</script>

<template>
  <div class="card card-pad text-center">
    <p class="section-title">App not found</p>
    <p class="help">It may have been deleted.</p>
    <RouterLink to="/apps" class="btn-secondary mt-4">Back to apps</RouterLink>
  </div>
</template>
