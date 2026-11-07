<script setup lang="ts">
// Shared "app not found / not yours" panel for the health and settings pages.
</script>

<template>
  <div class="sec sec-pad">
    <p class="section-title">App not found</p>
    <p class="help">It may have been deleted.</p>
    <RouterLink to="/apps" class="btn-secondary btn-sm mt-3">Back to apps</RouterLink>
  </div>
</template>
