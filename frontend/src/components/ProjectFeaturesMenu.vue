<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { AdminProject } from '../types'

const props = defineProps<{ project: AdminProject; busy?: boolean }>()
const emit = defineEmits<{ toggle: [field: 'kaisen_web_search' | 'kaisen_uploads', value: boolean] }>()
const open = ref(false)
const root = ref<HTMLElement | null>(null)
const btn = ref<HTMLButtonElement | null>(null)

const web = computed(() => props.project.kaisen_web_search_enabled !== false)
const uploads = computed(() => props.project.kaisen_uploads_enabled === true)
const onCount = computed(() => Number(web.value) + Number(uploads.value))

function onDoc(e: Event) {
  if (root.value && !root.value.contains(e.target as Node)) open.value = false
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    open.value = false
    btn.value?.focus()
  }
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
    <button ref="btn" type="button" class="row-act" aria-haspopup="true" :aria-expanded="open" :aria-label="`Features for ${project.name}: ${onCount} on`" @click="open = !open">
      Features ({{ onCount }} on)
    </button>
    <fieldset v-if="open" class="absolute right-0 z-20 mt-1 w-48 rounded-md border border-pico-border bg-pico-panel p-2 shadow-lg">
      <legend class="sr-only">Kaisen features for {{ project.name }}</legend>
      <label class="flex items-center gap-2 py-1 text-sm text-zinc-200">
        <input type="checkbox" class="check" :checked="web" :disabled="busy" @change="emit('toggle', 'kaisen_web_search', ($event.target as HTMLInputElement).checked)" /> Web search
      </label>
      <label class="flex items-center gap-2 py-1 text-sm text-zinc-200">
        <input type="checkbox" class="check" :checked="uploads" :disabled="busy" @change="emit('toggle', 'kaisen_uploads', ($event.target as HTMLInputElement).checked)" /> File uploads
      </label>
    </fieldset>
  </div>
</template>
