<script setup lang="ts">
// Logo + text wordmark. Used on the centered sign-in / status pages (the shell has its own in the sidebar).
// The logo is the brand mark, not decoration; the name beside it is the accessible text, so alt is empty.
const logo = `${import.meta.env.BASE_URL}icon-64.png`
</script>

<template>
  <span class="inline-flex items-center gap-2 text-base font-semibold tracking-tight text-white"><img :src="logo" alt="" width="20" height="20" class="h-5 w-5 rounded-sm" />PicoPaaS</span>
</template>
