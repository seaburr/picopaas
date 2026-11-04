<script setup lang="ts">
import { computed, onMounted, ref, useId, watch } from 'vue'
import { Paperclip } from 'lucide-vue-next'
import AttachChip from './AttachChip.vue'
import { listUploads } from '../api/kaisen'
import { ATTACH_ACCEPT, useAttachments } from '../composables/useAttachments'
import type { KaisenUpload, KaisenUploadsAvailability, KaisenUploadsSummary } from '../types'

const props = defineProps<{
  modelValue: string
  buildId: number | null // null: New build page, files are held until uploadHeld()
  summary?: KaisenUploadsAvailability & Partial<KaisenUploadsSummary>
  label: string // accessible name of the textarea
  inputId?: string // when the page has its own visible label for the textarea
  describedby?: string
  invalid?: boolean
  noSubmit?: boolean // the page has its own submit button
  unsent?: KaisenUpload[] // files already on the build that no run has sent yet
  placeholder?: string
  submitLabel?: string
  busy?: boolean
  locked?: string // non-empty: the whole composer is off (reason shown)
  canAttach: boolean // the user may change files right now (member, no active run)
  hint?: string
  rows?: number
}>()
const emit = defineEmits<{ 'update:modelValue': [v: string]; submit: [p: { text: string; attachmentIds: number[] }]; 'open-files': []; changed: [] }>()

const maxBytes = ref<number | undefined>(undefined)
onMounted(async () => {
  if (props.buildId === null) return
  try {
    maxBytes.value = (await listUploads(props.buildId as number)).quota.max_upload_bytes
  } catch {
    maxBytes.value = undefined // the server still enforces it
  }
})

// Why attaching is off, in plain words ('' when it is on).
const attachOff = computed(() => {
  if (props.locked) return ''
  if (props.summary && !props.summary.uploads_enabled) return props.summary.uploads_disabled_reason || props.summary.uploads_not_enabled_label || 'Not enabled for this project yet'
  if (!props.canAttach) return 'You can not change files right now.'
  return ''
})
const attachOn = computed(() => !props.locked && !attachOff.value)
const a = useAttachments({ buildId: () => props.buildId, enabled: () => attachOn.value, maxBytes: () => maxBytes.value, onChanged: () => emit('changed') })

const fileInput = ref<HTMLInputElement | null>(null)
const blockedMsg = ref('')
function pick() {
  if (!attachOn.value) {
    blockedMsg.value = attachOff.value
    return
  }
  fileInput.value?.click()
}
function take(files: File[]) {
  blockedMsg.value = ''
  if (!files.length) return
  if (a.addFiles(files) === 'blocked') blockedMsg.value = attachOff.value || 'Attaching is off right now.'
}
function onInput(e: Event) {
  const el = e.target as HTMLInputElement
  take(Array.from(el.files ?? []))
  el.value = ''
}
function onPaste(e: ClipboardEvent) {
  const files = Array.from(e.clipboardData?.files ?? [])
  if (!files.length) return
  e.preventDefault()
  take(files)
}
const dragging = ref(false)
let depth = 0
function onDragEnter(e: DragEvent) {
  if (!e.dataTransfer?.types.includes('Files')) return
  depth++
  dragging.value = true
}
function onDragLeave() {
  depth = Math.max(0, depth - 1)
  if (!depth) dragging.value = false
}
function onDrop(e: DragEvent) {
  depth = 0
  dragging.value = false
  take(Array.from(e.dataTransfer?.files ?? []))
}

const text = computed({ get: () => props.modelValue, set: (v: string) => emit('update:modelValue', v) })
const canSend = computed(() => !props.locked && !props.busy && !!props.modelValue.trim() && a.uploading.value === 0)
const sendHint = computed(() => {
  if (a.uploading.value) return 'Send is available once uploads finish.'
  if (a.rejected.value) return `${a.rejected.value} ${a.rejected.value === 1 ? 'file can not' : 'files can not'} be sent. Remove ${a.rejected.value === 1 ? 'it' : 'them'} or ignore.`
  return props.hint ?? ''
})
function submit() {
  if (!canSend.value) return
  emit('submit', { text: props.modelValue.trim(), attachmentIds: a.readyIds.value })
  a.clear()
}
// A bare Enter in the textarea keeps its newline; Ctrl/Cmd+Enter sends.
function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault()
    submit()
  }
}
watch(() => props.locked, (v) => v && a.clear())
const count = computed(() => props.summary?.count)
watch(() => [props.unsent, props.locked] as const, ([u, l]) => { if (u && !l) a.hydrate(u) }, { immediate: true, deep: true })
const hasUnsent = computed(() => a.chips.value.some((c) => c.status === 'ready'))
defineExpose({ uploadHeld: a.uploadHeld, chips: a.chips, held: a.held, rejected: a.rejected, failed: a.failed, uploading: a.uploading })
const uid = useId()
const offHelp = computed(() => (props.summary && !props.summary.uploads_enabled ? `${attachOff.value.replace(/[.\s]+$/, '')}. ${/\bask\b/i.test(attachOff.value) ? '' : 'Ask a platform admin to turn on file uploads. '}You can still describe images in words.` : attachOff.value))
</script>

<template>
  <!-- eslint-disable-next-line vuejs-accessibility/no-static-element-interactions -- drag and drop is an extra path; the Attach button is the keyboard control -->
  <div class="relative" @dragenter.prevent="onDragEnter" @dragover.prevent @dragleave="onDragLeave" @drop.prevent="onDrop">
    <div v-if="dragging" class="absolute inset-0 z-10 flex items-center justify-center rounded-md border-2 border-dashed border-accent-light bg-pico-bg/90 text-sm text-accent-light" aria-live="polite">Drop to attach (png, jpg, webp, svg, css, html, txt, md)</div>
    <label v-if="!inputId" class="sr-only" :for="uid">{{ label }}</label>
    <textarea :id="inputId ?? uid" v-model="text" class="input" :rows="rows ?? 3" :disabled="!!locked || busy" :placeholder="placeholder" :aria-describedby="describedby" :aria-invalid="invalid ? 'true' : undefined" @paste="onPaste" @keydown="onKey" />
    <ul v-if="a.chips.value.length" class="mt-2 space-y-2" aria-label="Attached files">
      <AttachChip v-for="c in a.chips.value" :key="c.key" :chip="c" @remove="a.remove(c.key)" @retry="a.retry(c.key)" @note="a.setNote(c.key, $event)" @save="a.saveNote(c.key)" @open-files="emit('open-files')" />
    </ul>
    <div class="sr-only" aria-live="polite">{{ a.uploading.value ? 'Uploading files' : '' }}</div>
    <p v-if="locked" class="help mt-1">{{ locked }}</p>
    <div class="mt-2 flex flex-wrap items-center gap-2">
      <input ref="fileInput" type="file" multiple class="sr-only" tabindex="-1" aria-hidden="true" :accept="ATTACH_ACCEPT" @change="onInput" />
      <button type="button" class="btn-secondary btn-sm" :aria-disabled="!attachOn || undefined" :title="attachOff || 'Attach files'" :class="attachOn ? '' : 'opacity-50'" @click="pick">
        <Paperclip :size="14" aria-hidden="true" />Attach
      </button>
      <button v-if="summary && buildId !== null && summary.max_files !== undefined" type="button" class="btn-secondary btn-sm" @click="emit('open-files')">
        Files<span class="meta ml-1">{{ count }} of {{ summary.max_files }}</span><span v-if="summary.changed_since_deploy" class="ml-1.5 inline-block h-2 w-2 rounded-full bg-amber-400" aria-hidden="true" /><span v-if="summary.changed_since_deploy" class="sr-only">, changed since deploy</span>
      </button>
      <span v-if="attachOff" class="help min-w-0 basis-full sm:basis-0 sm:flex-1">{{ offHelp }}</span>
      <span v-else class="flex-1" />
      <span v-if="sendHint" class="help">{{ sendHint }}</span>
      <button v-if="!noSubmit" type="button" class="btn-primary ml-auto" :disabled="!canSend" @click="submit">{{ busy ? 'Sending…' : submitLabel }}</button>
    </div>
    <p v-if="hasUnsent && buildId !== null" class="help mt-1">Attached files are sent with your next message.</p>
    <p v-if="blockedMsg && !attachOff" class="field-error mt-1" role="alert">{{ blockedMsg }}</p>
  </div>
</template>
