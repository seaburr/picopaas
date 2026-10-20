<script setup lang="ts">
defineProps<{ slim?: boolean }>()
</script>

<template>
  <div
    role="note"
    class="alert-warn"
    :class="slim ? 'rounded-none border-x-0 border-t-0 px-4 py-1 text-center text-xs' : ''"
  >
    <strong>Dev/test platform.</strong> Accounts are approved by hand. No uptime guarantees; don't store personal data.
  </div>
</template>
