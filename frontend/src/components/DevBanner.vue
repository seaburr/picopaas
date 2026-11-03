<script setup lang="ts">
defineProps<{ slim?: boolean }>()
</script>

<template>
  <div
    role="note"
    class="alert-warn"
    :class="slim ? 'rounded-none border-x-0 border-t-0 px-5 py-1.5 text-center text-sm leading-relaxed' : ''"
  >
    <strong>Dev/test platform.</strong> Sign up, and if your account is approved, a PicoPaaS platform admin will reach out. No uptime guarantees; don't store personal data.
  </div>
</template>
