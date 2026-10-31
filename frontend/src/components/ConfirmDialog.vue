<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { useModal } from '../composables/useModal'

// <ConfirmDialog :open="x" title="Delete app?" message="..." danger confirm-label="Delete app"
//                require-text="my-app" @confirm="..." @cancel="x = false" />
// require-text: the user must type that exact text before Confirm enables (use for destructive deletes).
// Slot: extra content under the message. `busy` disables both buttons and shows busyLabel.
const props = defineProps<{
  open: boolean
  title: string
  message: string
  confirmLabel?: string
  danger?: boolean
  requireText?: string
  busy?: boolean
  busyLabel?: string
  confirmDisabled?: boolean // extra condition from the slot (e.g. a required reason is empty)
}>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()

const dlg = ref<HTMLDialogElement | null>(null)
const typed = ref('')
const uid = useId()
const titleId = `${uid}-title`
const msgId = `${uid}-msg`
const inputId = `${uid}-confirm`
const ok = computed(() => (!props.requireText || typed.value.trim() === props.requireText) && !props.confirmDisabled)
watch(() => props.open, () => (typed.value = ''))

// Danger confirms start on Cancel (safe default); typed confirms start in the input; others on Confirm.
const m = useModal(dlg, () => props.open, () => !props.busy && emit('cancel'))
function submit() {
  if (ok.value && !props.busy) emit('confirm')
}
</script>

<template>
  <dialog
    ref="dlg"
    :aria-labelledby="titleId"
    :aria-describedby="msgId"
    class="m-auto w-[calc(100%-2rem)] max-w-sm rounded-lg border border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/70"
    @cancel="m.onNativeCancel"
    @click="m.onBackdrop"
  >
    <form class="p-4" @submit.prevent="submit">
      <h2 :id="titleId" class="section-title">{{ title }}</h2>
      <p :id="msgId" class="mt-2 text-sm text-zinc-300">{{ message }}</p>
      <slot />
      <div v-if="requireText" class="mt-4">
        <label :for="inputId" class="label">
          Type <strong class="break-all font-mono text-white">{{ requireText }}</strong> to confirm
        </label>
        <input
          :id="inputId"
          v-model="typed"
          type="text"
          class="input font-mono"
          autocomplete="off"
          autocapitalize="off"
          spellcheck="false"
          data-autofocus
        />
      </div>
      <div class="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <button type="button" class="btn-secondary" :data-autofocus="!requireText && danger ? '' : undefined" :disabled="busy" @click="emit('cancel')">
          Cancel
        </button>
        <button
          type="submit"
          :class="danger ? 'btn-danger-solid' : 'btn-primary'"
          :data-autofocus="!requireText && !danger ? '' : undefined"
          :disabled="!ok || busy"
        >
          {{ busy ? (busyLabel ?? 'Working…') : (confirmLabel ?? 'Confirm') }}
        </button>
      </div>
    </form>
  </dialog>
</template>
