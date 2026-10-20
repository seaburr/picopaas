<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'

defineProps<{ label: string }>()
const open = ref(false)
const root = ref<HTMLElement | null>(null)

function onDoc(e: Event) {
  if (root.value && !root.value.contains(e.target as Node)) open.value = false
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') open.value = false
}
watch(open, (v) => {
  if (v) {
    document.addEventListener('click', onDoc)
    document.addEventListener('keydown', onKey)
  } else {
    document.removeEventListener('click', onDoc)
    document.removeEventListener('keydown', onKey)
  }
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDoc)
  document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div ref="root" class="relative">
    <button
      type="button"
      class="btn-ghost h-10 w-10 !min-h-0 !p-0"
      aria-haspopup="menu"
      :aria-expanded="open"
      :aria-label="label"
      @click="open = !open"
    >
      <svg class="h-5 w-5" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
        <circle cx="3" cy="8" r="1.4" /><circle cx="8" cy="8" r="1.4" /><circle cx="13" cy="8" r="1.4" />
      </svg>
    </button>
    <div
      v-if="open"
      role="menu"
      class="absolute right-0 z-20 mt-1 w-44 rounded-md border border-pico-border bg-pico-panel py-1 shadow-xl"
      @click="open = false"
    >
      <slot />
    </div>
  </div>
</template>
