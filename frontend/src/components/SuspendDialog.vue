<script setup lang="ts">
import { ref, useId, watch } from 'vue'
import ConfirmDialog from './ConfirmDialog.vue'

// Suspend dialog with a required reason. The parent runs the call and closes the dialog; `error` shows inside it.
const props = defineProps<{
  open: boolean
  title: string
  message: string
  confirmLabel: string
  hint: string
  placeholder: string
  busy?: boolean
  error?: string
}>()
const emit = defineEmits<{ confirm: [reason: string]; cancel: [] }>()

const reason = ref('')
const id = `${useId()}-reason`
watch(() => props.open, () => (reason.value = ''))
</script>

<template>
  <ConfirmDialog
    :open="open"
    :title="title"
    :message="message"
    :confirm-label="confirmLabel"
    busy-label="Suspending…"
    danger
    :busy="busy"
    :confirm-disabled="!reason.trim()"
    @confirm="emit('confirm', reason.trim())"
    @cancel="emit('cancel')"
  >
    <div class="mt-3">
      <label :for="id" class="label">Reason (required)</label>
      <textarea :id="id" v-model="reason" class="input min-h-20" rows="3" required :placeholder="placeholder" data-autofocus />
      <p class="help">{{ hint }}</p>
    </div>
    <p v-if="error" role="alert" class="field-error mt-2">{{ error }}</p>
  </ConfirmDialog>
</template>
