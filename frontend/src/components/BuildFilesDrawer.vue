<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { Pencil, RefreshCw, Trash2 } from 'lucide-vue-next'
import IconButton from './IconButton.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import { ApiError } from '../api/client'
import { deleteUpload, describeUpload, listUploads, replaceUpload, uploadContentUrl } from '../api/kaisen'
import { fmtSize } from '../composables/kaisen'
import { useModal } from '../composables/useModal'
import { useToastStore } from '../stores/toast'
import type { KaisenUpload, KaisenUploadList, KaisenUploadsSummary } from '../types'

const props = defineProps<{
  open: boolean
  focusId?: number | null // scroll to and mark this file when opened from a timeline tile
  buildId: number
  summary?: KaisenUploadsSummary
  canEdit: boolean // member who may change the build (not read-only, not mid-run)
  canDeploy: boolean
  deploying: boolean
}>()
const emit = defineEmits<{ changed: []; deploy: []; close: [] }>()
const dlg = ref<HTMLDialogElement | null>(null)
const m = useModal(dlg, () => props.open, () => emit('close'))
const toast = useToastStore()

const data = ref<KaisenUploadList | null>(null)
const loading = ref(true)
const loadError = ref('')
const error = ref('')
const errorEl = ref<HTMLElement | null>(null)
// Move focus to a new error so keyboard and screen reader users land on it.
watch(error, (v) => { if (v) nextTick(() => errorEl.value?.focus()) })
const busy = ref(false)

// Replace keeps the type, so the check is the server's size cap and the file's extension.
const ASSET_EXT = ['png', 'jpg', 'jpeg', 'webp', 'svg']
const REF_EXT = ['css', 'html', 'htm', 'txt', 'md']
const extOf = (n: string) => (n.includes('.') ? n.split('.').pop()!.toLowerCase() : '')


// Availability comes from the list response once loaded, else from the build detail.
const enabled = computed(() => data.value?.uploads_enabled ?? props.summary?.uploads_enabled ?? false)
const offReason = computed(() => data.value?.uploads_disabled_reason || props.summary?.uploads_disabled_reason || data.value?.uploads_not_enabled_label || props.summary?.uploads_not_enabled_label || 'Not enabled for this project yet')
const notice = computed(() => data.value?.uploads_notice || props.summary?.uploads_notice || data.value?.notice || '')
const quota = computed(() => data.value?.quota)
const items = computed(() => data.value?.uploads ?? [])
const controlsOn = computed(() => enabled.value && props.canEdit)
const changed = computed(() => props.summary?.changed_since_deploy === true)

async function load() {
  try {
    data.value = await listUploads(props.buildId)
    loadError.value = ''
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : "Couldn't load files."
  } finally {
    loading.value = false
    if (props.focusId) nextTick(() => document.getElementById(`fd-u-${props.focusId}`)?.scrollIntoView({ block: 'center' }))
  }
}
watch(() => [props.open, props.buildId], () => { if (props.open) load() }, { immediate: true })

function friendly(e: unknown, fallback: string): string {
  if (!(e instanceof ApiError)) return fallback
  switch (e.status) {
    case 0: return e.message
    case 403: return e.message || 'Uploads are not available for this build right now.'
    case 409: return e.message || 'Wait for the current run to finish, or stop it, then try again.'
    case 413: return e.message || 'That file is too large.'
    case 415: return e.message || "That file type isn't supported."
    case 422: return e.message || "That file couldn't be accepted."
    case 429: return 'Too many uploads for now. Wait a while and try again.'
    case 503: return 'Image processing is busy. Try again in a moment.'
    case 504: return 'Image processing took too long. Try again, or use a smaller image.'
    default: return e.message || fallback
  }
}

// Returns a message when the file can't be right, else ''.
function precheck(f: File, k: 'asset' | 'reference'): string {
  const max = quota.value?.max_upload_bytes
  if (max && f.size > max) return `That file is too large. The most allowed is ${fmtSize(max)}.`
  if (f.size === 0) return 'That file is empty.'
  const ext = extOf(f.name)
  const ok = k === 'asset' ? ASSET_EXT : REF_EXT
  if (!ok.includes(ext)) return k === 'asset' ? 'Site images must be PNG, JPEG, WebP or SVG.' : 'Reference files must be CSS, HTML, TXT or MD.'
  return ''
}

// ---- replace ----
const replaceInput = ref<HTMLInputElement | null>(null)
const replaceTarget = ref<KaisenUpload | null>(null)
function startReplace(u: KaisenUpload) {
  replaceTarget.value = u
  replaceInput.value?.click()
}
async function onReplace(e: Event) {
  const el = e.target as HTMLInputElement
  const f = el.files?.[0]
  const u = replaceTarget.value
  el.value = ''
  if (!f || !u) return
  error.value = ''
  const msg = precheck(f, u.kind)
  if (msg) {
    error.value = msg
    return
  }
  busy.value = true
  try {
    await replaceUpload(props.buildId, u.id, f)
    toast.success(`Replaced ${u.name_orig}.`)
    await load()
    emit('changed')
  } catch (err) {
    error.value = friendly(err, "Couldn't replace the file.")
  } finally {
    busy.value = false
  }
}

// ---- describe ----
const editId = ref<number | null>(null)
const editText = ref('')
function startEdit(u: KaisenUpload) {
  editId.value = u.id
  editText.value = u.description
}
async function saveEdit(u: KaisenUpload) {
  error.value = ''
  busy.value = true
  try {
    await describeUpload(props.buildId, u.id, editText.value.trim())
    editId.value = null
    await load()
    emit('changed')
  } catch (e) {
    error.value = friendly(e, "Couldn't save the description.")
  } finally {
    busy.value = false
  }
}

// ---- delete (allowed even when uploads are off) ----
const delTarget = ref<KaisenUpload | null>(null)
async function confirmDelete() {
  const u = delTarget.value
  if (!u) return
  busy.value = true
  error.value = ''
  try {
    await deleteUpload(props.buildId, u.id)
    delTarget.value = null
    toast.success(`Removed ${u.name_orig}.`)
    await load()
    emit('changed')
  } catch (e) {
    delTarget.value = null
    error.value = friendly(e, "Couldn't remove the file.")
  } finally {
    busy.value = false
  }
}

// Any non-zero use shows at least a sliver; exactly zero is an empty bar.
const pct = (used: number, limit: number) => (used <= 0 || !limit ? 0 : Math.min(100, Math.max(3, (used / limit) * 100)))
const dims = (u: KaisenUpload) => (u.width && u.height ? `${u.width}×${u.height}` : '')
</script>

<template>
  <dialog
    ref="dlg"
    aria-labelledby="h-uploads"
    class="fixed inset-y-0 right-0 left-auto m-0 h-dvh max-h-dvh w-full overflow-y-auto border-l border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/60 sm:max-w-[460px] max-sm:inset-x-0 max-sm:top-auto max-sm:bottom-0 max-sm:h-auto max-sm:max-h-[85dvh] max-sm:max-w-none max-sm:rounded-t-lg max-sm:border-l-0 max-sm:border-t"
    @cancel="m.onNativeCancel"
    @click="m.onBackdrop"
  >
  <section class="space-y-3 p-4">
    <div class="flex flex-wrap items-baseline justify-between gap-2">
      <h2 id="h-uploads" class="section-title">Files for this build</h2>
      <button type="button" class="btn-secondary btn-sm" data-autofocus @click="emit('close')">Close</button>
    </div>
    <p v-if="quota" class="meta">{{ quota.count }} of {{ quota.max_files }} files · project {{ fmtSize(quota.project_bytes) }} of {{ fmtSize(quota.project_limit_bytes) }} · per file {{ fmtSize(quota.max_upload_bytes) }}</p>
    <p v-if="!enabled" class="alert-info" role="status">{{ offReason.replace(/[.\s]+$/, '') }}. You can still remove files you uploaded earlier.</p>
    <p v-else-if="!canEdit" class="help">You can't change files while a run is active or in a read-only state.</p>
    <p v-if="notice" class="help">{{ notice }}</p>

    <p v-if="loading" class="meta" aria-busy="true">Loading files…</p>
    <div v-else-if="loadError" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
      <span>{{ loadError }}</span>
      <button type="button" class="btn-secondary btn-sm" @click="load">Retry</button>
    </div>

    <template v-else>
      <div v-if="quota" class="grid gap-2 sm:grid-cols-2">
        <div v-for="b in [{ id: 'asset', label: 'Site images', used: quota.asset_bytes, limit: quota.asset_limit_bytes }, { id: 'ref', label: 'Reference files', used: quota.ref_bytes, limit: quota.ref_limit_bytes }]" :key="b.id">
          <div class="flex justify-between text-xs text-zinc-300"><span>{{ b.label }}</span><span>{{ fmtSize(b.used) }} of {{ fmtSize(b.limit) }}</span></div>
          <div class="mt-1 h-1.5 overflow-hidden rounded-full bg-pico-card" role="progressbar" :aria-label="b.label" aria-valuemin="0" :aria-valuemax="b.limit" :aria-valuenow="b.used">
            <div class="h-full rounded-full" :class="pct(b.used, b.limit) >= 80 ? 'bg-warn-solid' : 'bg-accent-light'" :style="{ width: `${pct(b.used, b.limit)}%` }" />
          </div>
        </div>
      </div>

      <p v-if="error" ref="errorEl" tabindex="-1" class="field-error whitespace-pre-wrap focus:outline-none" role="alert">{{ error }}</p>
      <span class="sr-only" aria-live="polite">{{ busy ? 'Working' : '' }}</span>

      <p v-if="items.length === 0" class="meta">No files yet.</p>
      <ul v-else class="divide-y divide-pico-line">
        <li v-for="u in items" :id="`fd-u-${u.id}`" :key="u.id" class="flex flex-wrap items-start gap-3 py-2" :class="focusId === u.id ? 'rounded-md bg-pico-card px-2' : ''">
          <img v-if="u.kind === 'asset'" :src="uploadContentUrl(buildId, u.id, u.version)" alt="" class="h-12 w-12 shrink-0 rounded-sm bg-pico-card object-contain" loading="lazy" />
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-x-2 gap-y-0.5">
              <span class="break-all text-sm font-medium text-zinc-100">{{ u.name_orig }}</span>
              <span class="badge-neutral">{{ u.kind === 'asset' ? 'Site' : 'Reference' }}</span>
              <span v-if="!u.run_id" class="badge-info">Not sent yet</span>
            </div>
            <p class="meta mono flex flex-wrap gap-x-2"><span class="break-all">{{ u.path }}</span><span>{{ u.ext.toUpperCase() }}</span><span>{{ fmtSize(u.size) }}</span><span v-if="dims(u)">{{ dims(u) }}</span></p>
            <form v-if="editId === u.id" class="mt-1 flex flex-wrap items-center gap-2" @submit.prevent="saveEdit(u)">
              <label :for="`bu-ed-${u.id}`" class="sr-only">Description for {{ u.name_orig }}</label>
              <input :id="`bu-ed-${u.id}`" v-model="editText" class="input min-w-0 flex-1" maxlength="200" />
              <button type="submit" class="btn-secondary btn-sm" :disabled="busy">Save</button>
              <button type="button" class="btn-ghost btn-sm" @click="editId = null">Cancel</button>
            </form>
            <p v-else class="text-sm text-zinc-300 break-words">{{ u.description || 'No description.' }}</p>
          </div>
          <div class="flex shrink-0 items-center gap-1">
            <IconButton :icon="Pencil" :label="`Edit description of ${u.name_orig}`" touch :disabled="!canEdit || busy" @click="startEdit(u)" />
            <IconButton :icon="RefreshCw" :label="`Replace ${u.name_orig}`" touch :disabled="!controlsOn || busy" @click="startReplace(u)" />
            <IconButton :icon="Trash2" :label="`Delete ${u.name_orig}`" danger touch :disabled="!canEdit || busy" @click="delTarget = u" />
          </div>
        </li>
      </ul>
      <input ref="replaceInput" type="file" class="sr-only" tabindex="-1" aria-hidden="true" accept=".png,.jpg,.jpeg,.webp,.svg,.css,.html,.htm,.txt,.md" @change="onReplace" />

      <div v-if="changed" class="alert-info flex flex-wrap items-center justify-between gap-2" role="status">
        <span>Files changed, deploy to publish them.</span>
        <button v-if="canDeploy" type="button" class="btn-secondary btn-sm" :disabled="deploying" @click="emit('deploy')">{{ deploying ? 'Deploying…' : 'Deploy now' }}</button>
      </div>
    </template>

    <ConfirmDialog
      :open="delTarget !== null"
      title="Remove file"
      :message="`Remove ${delTarget?.name_orig ?? 'this file'}? Kaisen stops seeing it. Deploy again to take it out of the live app.`"
      confirm-label="Remove file"
      danger
      :busy="busy"
      @confirm="confirmDelete"
      @cancel="delTarget = null"
    />
  </section>
  </dialog>
</template>
