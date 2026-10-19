<script setup lang="ts">
defineProps<{ slim?: boolean }>()
</script>

<template>
  <div
    role="note"
    class="border border-amber-500/40 bg-amber-500/10 text-amber-200"
    :class="slim ? 'border-x-0 border-t-0 px-4 py-1 text-center text-xs' : 'rounded-md px-3 py-2 text-sm'"
  >
    <strong>Development / test platform.</strong> Invite only. No uptime guarantees; apps and data may be deleted at
    any time. Don't store personal or sensitive data here.
  </div>
</template>
