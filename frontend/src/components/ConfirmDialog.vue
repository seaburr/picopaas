<script setup lang="ts">
defineProps<{
  open: boolean
  title: string
  message: string
  confirmLabel?: string
  danger?: boolean
}>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()
</script>

<template>
  <div v-if="open" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4">
    <div class="w-full max-w-sm rounded-lg border border-pico-border bg-pico-panel p-5 shadow-xl">
      <h2 class="text-base font-semibold text-white">{{ title }}</h2>
      <p class="mt-2 text-sm text-zinc-400">{{ message }}</p>
      <div class="mt-5 flex justify-end gap-2">
        <button
          type="button"
          class="rounded-md border border-pico-border px-3 py-1.5 text-sm text-zinc-300 hover:bg-pico-card"
          @click="emit('cancel')"
        >
          Cancel
        </button>
        <button
          type="button"
          :class="[
            'rounded-md px-3 py-1.5 text-sm font-medium text-white',
            danger ? 'bg-danger-dark hover:bg-danger' : 'bg-accent-dark hover:bg-accent',
          ]"
          @click="emit('confirm')"
        >
          {{ confirmLabel ?? 'Confirm' }}
        </button>
      </div>
    </div>
  </div>
</template>
