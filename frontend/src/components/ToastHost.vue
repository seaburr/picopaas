<script setup lang="ts">
import { X } from 'lucide-vue-next'
import IconButton from './IconButton.vue'
import { useToastStore } from '../stores/toast'

const toast = useToastStore()
const tone = {
  success: 'border-emerald-500/50 text-emerald-200',
  error: 'border-danger/60 text-red-200',
  info: 'border-accent/60 text-blue-100',
} as const
const glyph = { success: '✓', error: '!', info: 'i' } as const
</script>

<template>
  <div class="pointer-events-none fixed inset-x-0 bottom-0 z-60 flex flex-col items-center gap-2 px-4 pb-4 sm:items-end sm:px-6">
    <div
      v-for="t in toast.toasts"
      :key="t.id"
      :role="t.kind === 'error' ? 'alert' : 'status'"
      :class="['pointer-events-auto flex w-full max-w-sm items-start gap-2.5 rounded-lg border bg-pico-card px-3 py-2.5 text-sm shadow-xl', tone[t.kind]]"
    >
      <span aria-hidden="true" class="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-current text-xs font-bold">{{ glyph[t.kind] }}</span>
      <p class="min-w-0 flex-1 wrap-break-word">{{ t.message }}</p>
      <IconButton :icon="X" label="Dismiss notification" sm @click="toast.dismiss(t.id)" />
    </div>
  </div>
</template>
