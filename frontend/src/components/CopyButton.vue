<script setup lang="ts">
import { ref } from 'vue'
import { useToastStore } from '../stores/toast'

const props = defineProps<{ text: string; label?: string }>()
const toast = useToastStore()
const done = ref(false)

async function copy() {
  try {
    await navigator.clipboard.writeText(props.text)
    done.value = true
    setTimeout(() => (done.value = false), 1500)
  } catch {
    toast.error("Couldn't copy. Select the text and copy it manually.")
  }
}
</script>

<template>
  <button
    type="button"
    class="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-zinc-400 hover:bg-pico-card hover:text-white"
    :title="done ? 'Copied' : (label ?? 'Copy')"
    :aria-label="label ?? 'Copy'"
    @click="copy"
  >
    <svg v-if="!done" class="h-4 w-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
      <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" /><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2" />
    </svg>
    <svg v-else class="h-4 w-4 text-emerald-300" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
      <path d="M3 8.5l3.2 3L13 4.5" />
    </svg>
    <span class="sr-only" aria-live="polite">{{ done ? 'Copied' : '' }}</span>
  </button>
</template>
