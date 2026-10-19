<script setup lang="ts">
import { RouterView } from 'vue-router'
import DevBanner from './components/DevBanner.vue'
</script>

<template>
  <DevBanner slim />
  <RouterView />
</template>
