<script setup lang="ts">
import { useToastStore } from '../stores/toast'

const toast = useToastStore()
// State-coloured text marker (dot shape differs per tone) + the message; the message itself stays plain ink.
const tone = { success: 'ok', error: 'err', info: 'off' } as const
const word = { success: 'Done', error: 'Error', info: 'Note' } as const
</script>

<template>
  <div class="pointer-events-none fixed inset-x-0 bottom-0 z-60 flex flex-col items-center gap-2 px-4 pb-4 sm:items-end sm:px-6">
    <div
      v-for="t in toast.toasts"
      :key="t.id"
      :role="t.kind === 'error' ? 'alert' : 'status'"
      class="pointer-events-auto flex w-full max-w-sm items-start gap-2.5 rounded-md border border-pico-border bg-pico-panel px-3 py-2 text-sm text-white shadow-lg"
    >
      <span :class="['st mt-px shrink-0 text-xs font-medium', `tone-${tone[t.kind]}`]">
        <span :class="['dot', `dot-${tone[t.kind]}`]" aria-hidden="true" />{{ word[t.kind] }}
      </span>
      <p class="min-w-0 flex-1 wrap-break-word">{{ t.message }}</p>
      <button type="button" class="row-act shrink-0 text-xs" aria-label="Dismiss notification" @click="toast.dismiss(t.id)">Dismiss</button>
    </div>
  </div>
</template>
