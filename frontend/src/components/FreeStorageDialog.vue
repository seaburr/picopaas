<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import ConfirmDialog from './ConfirmDialog.vue'

// Frees blob storage space with the platform's credentials. Delete everything, or only objects under a prefix.
const props = defineProps<{ open: boolean; busy?: boolean }>()
const emit = defineEmits<{ confirm: [body: { prefix: string } | { all: true }]; cancel: [] }>()

const mode = ref<'all' | 'prefix'>('all')
const prefix = ref('')
watch(() => props.open, () => {
  mode.value = 'all'
  prefix.value = ''
})
const valid = computed(() => mode.value === 'all' || prefix.value.length > 0)
// ConfirmDialog only enables Confirm once this exact text is typed; an invalid prefix makes it unmatchable.
const confirmText = computed(() => (valid.value ? 'delete' : 'enter a prefix first'))
function confirm() {
  if (!valid.value) return
  emit('confirm', mode.value === 'all' ? { all: true } : { prefix: prefix.value })
}
</script>

<template>
  <ConfirmDialog
    :open="open"
    title="Free up blob storage space"
    message="Deleted objects can't be recovered. Write access comes back as soon as the bucket is under its cap, and the app restarts to pick up the new keys."
    confirm-label="Delete objects"
    busy-label="Deleting…"
    danger
    :busy="busy"
    :require-text="confirmText"
    @confirm="confirm"
    @cancel="emit('cancel')"
  >
    <fieldset class="mt-3 space-y-2 text-sm">
      <legend class="sr-only">What to delete</legend>
      <label class="flex items-center gap-2 text-zinc-200"><input v-model="mode" type="radio" value="all" class="check" /> Delete all objects</label>
      <label class="flex items-center gap-2 text-zinc-200"><input v-model="mode" type="radio" value="prefix" class="check" /> Only objects under a prefix</label>
      <input
        v-if="mode === 'prefix'"
        v-model="prefix"
        type="text"
        class="input mono"
        placeholder="uploads/old/"
        aria-label="Key prefix"
        autocomplete="off"
        spellcheck="false"
        maxlength="1024"
      />
    </fieldset>
  </ConfirmDialog>
</template>
