<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { MoreHorizontal } from 'lucide-vue-next'

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
      class="btn-icon"
      aria-haspopup="menu"
      :aria-expanded="open"
      :aria-label="label"
      :title="label"
      @click="open = !open"
    >
      <MoreHorizontal :size="16" aria-hidden="true" />
    </button>
    <!-- eslint-disable-next-line vuejs-accessibility/click-events-have-key-events, vuejs-accessibility/interactive-supports-focus -- click on a bubbled item button closes the menu; items are real buttons, Escape handled by the parent -->
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
