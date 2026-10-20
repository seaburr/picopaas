<script setup lang="ts">
import { ref } from 'vue'
import { Check, Copy } from 'lucide-vue-next'
import { useToastStore } from '../stores/toast'

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
    class="btn-icon"
    :class="sm ? 'btn-icon-sm' : ''"
    :title="done ? 'Copied' : (label ?? 'Copy')"
    :aria-label="label ?? 'Copy'"
    @click="copy"
  >
    <Copy v-if="!done" :size="16" aria-hidden="true" />
    <Check v-else :size="16" class="text-emerald-300" aria-hidden="true" />
    <span class="sr-only" aria-live="polite">{{ done ? 'Copied' : '' }}</span>
  </button>
</template>
