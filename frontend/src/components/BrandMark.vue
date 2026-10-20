<script setup lang="ts">
// Slingshot icon + wordmark. size: icon px (NavBar 28, auth cards 40).
withDefaults(defineProps<{ size?: number }>(), { size: 28 })
const ICON = '/static/icon-64.png' // public/ asset; base is /static/ in dev and prod
</script>

<template>
  <span class="inline-flex items-center gap-2 font-bold tracking-tight text-white">
    <img :src="ICON" alt="" :width="size" :height="size" class="rounded-md" :style="{ width: size + 'px', height: size + 'px' }" />
    <span :class="size > 32 ? 'text-xl' : 'text-base'">PicoPaaS</span>
  </span>
</template>
