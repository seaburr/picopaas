<script setup lang="ts">
import { ref } from 'vue'
import { useToastStore } from '../stores/toast'

// Text action ("Copy" -> "Copied"). `sm` is accepted for old call sites and ignored.
const props = defineProps<{ text: string; label?: string; sm?: boolean }>()
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
    class="row-act"
    :title="done ? 'Copied' : (label ?? 'Copy')"
    :aria-label="label ?? 'Copy'"
    @click="copy"
  >
    <span aria-hidden="true">{{ done ? 'Copied' : 'Copy' }}</span><span class="sr-only" aria-live="polite">{{ done ? 'Copied' : '' }}</span>
  </button>
</template>
