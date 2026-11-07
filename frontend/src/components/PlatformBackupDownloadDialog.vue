<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import { getPlatformBackupExport, startPlatformBackupDownload } from '../api/admin'
import { ApiError } from '../api/client'
import { useModal } from '../composables/useModal'
import { formatBytes, stamp } from '../utils/format'
import PasswordField from './PasswordField.vue'
import type { PlatformBackupExport, PlatformBackupRun } from '../types'

// Download one platform database backup. The admin re-enters their password; the server prepares the file and we poll.
// The password and the signed link live only in this component's memory: never logged, stored or opened automatically.
const props = defineProps<{ run: PlatformBackupRun | null }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const POLL_MS = 3000
const uid = useId()
const dlg = ref<HTMLDialogElement | null>(null)
const m = useModal(dlg, () => !!props.run, () => emit('close'))

const password = ref('')
const busy = ref(false)
const error = ref('')
const ex = ref<PlatformBackupExport | null>(null)
let timer: number | undefined
let gen = 0 // invalidates in-flight polls when the dialog closes or restarts

function stop() {
  gen++
  window.clearTimeout(timer)
}
onBeforeUnmount(stop)

const STATUS_ERR: Record<number, string> = {
  403: "That password is wrong, or your account isn't allowed to download backups.",
  404: 'This backup no longer exists.',
  409: 'Another backup download is being prepared. Wait for it to finish, then try again.',
  503: 'Downloads are unavailable right now. Try again later.',
}
function plain(e: unknown, fallback: string): string {
  if (!(e instanceof ApiError)) return fallback
  if (e.status === 429 || e.status === 422) return e.message // carries the wait time or the exact reason
  return STATUS_ERR[e.status] ?? e.message
}

async function poll(id: number, my: number) {
  try {
    const next = await getPlatformBackupExport(id)
    if (my !== gen) return
    ex.value = next
    if (next.status !== 'queued' && next.status !== 'running') emit('changed')
    if (next.status === 'queued' || next.status === 'running') timer = window.setTimeout(() => poll(id, my), POLL_MS)
    else if (next.status === 'ready' && !next.url) timer = window.setTimeout(() => poll(id, my), POLL_MS)
  } catch (e) {
    if (my !== gen) return
    if (e instanceof ApiError && e.status === 404) {
      // Not ours (another admin's) or gone: back to the start state instead of an error.
      ex.value = null
      emit('changed')
      return
    }
    error.value = plain(e, "Couldn't check the download.")
  }
}

// Open (or reopen): resume a live export, otherwise show the password form.
watch(
  () => props.run,
  (r) => {
    stop()
    password.value = ''
    error.value = ''
    ex.value = null
    busy.value = false
    const live = r?.export
    if (r && live && (live.status === 'queued' || live.status === 'running' || live.status === 'ready')) {
      ex.value = { export_id: live.id, run_id: r.id, status: live.status, created_at: '', available_until: live.available_until }
      void poll(live.id, gen)
    }
  },
  { immediate: true },
)

async function submit() {
  if (!props.run || !password.value || busy.value) return
  busy.value = true
  error.value = ''
  stop()
  const my = gen
  try {
    ex.value = await startPlatformBackupDownload(props.run.id, password.value)
    password.value = ''
    emit('changed')
    timer = window.setTimeout(() => poll(ex.value!.export_id, my), POLL_MS)
  } catch (e) {
    error.value = plain(e, "Couldn't start the download.")
  } finally {
    busy.value = false
  }
}
function again() {
  stop()
  ex.value = null
  error.value = ''
}

const working = computed(() => ex.value?.status === 'queued' || ex.value?.status === 'running' || (ex.value?.status === 'ready' && !ex.value.url))
</script>

<template>
  <dialog
    ref="dlg"
    :aria-labelledby="`${uid}-title`"
    class="m-auto w-[calc(100%-2rem)] max-w-lg rounded-md border border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/70"
    @cancel="m.onNativeCancel"
    @click="m.onBackdrop"
  >
    <form v-if="run" class="p-4" autocomplete="off" @submit.prevent="submit">
      <h2 :id="`${uid}-title`" class="section-title">Download platform backup</h2>
      <p class="help">Backup #{{ run.id }} from {{ stamp(run.finished_at ?? run.created_at) }}</p>

      <template v-if="!ex">
        <div class="alert-warn mt-3 text-xs" role="note">
          This file contains every account's password hash, all encrypted app secrets and audit data. Store it somewhere safe and delete it when you're done.
          The file is available for 15 minutes, only to you (the admin who started it). Each download link lasts up to 10 minutes. The file is removed from storage within about an hour.
        </div>
        <div class="mt-3">
          <PasswordField v-model="password" label="Your password" autocomplete="current-password" help="Enter your own admin password to continue." />
        </div>
      </template>

      <div v-else-if="working" class="mt-4 text-sm text-zinc-300" role="status" aria-live="polite">
        <p>{{ ex.status === 'queued' ? 'Waiting to start…' : 'Preparing your file…' }}</p>
        <p class="help">You can close this window. The file keeps preparing and this backup will show its state.</p>
      </div>

      <div v-else-if="ex.status === 'ready' && ex.url" class="mt-4 space-y-2 text-sm" role="status">
        <p class="text-zinc-200">Your file is ready<template v-if="ex.size_bytes"> ({{ formatBytes(ex.size_bytes) }})</template>.</p>
        <a :href="ex.url" :download="ex.filename || undefined" rel="noopener noreferrer" class="btn-primary inline-flex">Download {{ ex.filename || 'backup' }}</a>
        <p class="help">This link works for up to 10 minutes; the file stays available<template v-if="ex.available_until"> until {{ stamp(ex.available_until) }}</template>. Close this window to hide it.</p>
      </div>

      <div v-else-if="ex.status === 'failed'" class="alert-error mt-4 text-sm" role="alert">
        The download failed{{ ex.error ? `: ${ex.error}` : '.' }}
      </div>

      <div v-else class="alert-info mt-4 text-sm" role="status">This download has expired (it is available for 15 minutes). Start a new one.</div>

      <p v-if="error" class="alert-error mt-3 text-xs" role="alert">{{ error }}</p>

      <div class="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <button type="button" class="btn-secondary" @click="emit('close')">Close</button>
        <button v-if="!ex" type="submit" class="btn-primary" :disabled="!password || busy" :aria-busy="busy">{{ busy ? 'Starting…' : 'Prepare download' }}</button>
        <button v-else-if="ex.status === 'failed' || ex.status === 'expired'" type="button" class="btn-primary" @click="again">Start over</button>
      </div>
    </form>
  </dialog>
</template>
