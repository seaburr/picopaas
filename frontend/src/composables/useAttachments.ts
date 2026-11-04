import { computed, ref } from 'vue'
import { ApiError } from '../api/client'
import { addUpload, deleteUpload, describeUpload, uploadContentUrl } from '../api/kaisen'
import { fmtSize } from './kaisen'
import type { KaisenUpload } from '../types'

// Attachment queue for a composer.
//  - build mode (buildId set): every file uploads immediately.
//  - held mode (buildId null, the New build page): files stay in the browser until uploadHeld(buildId).
const ASSET_EXT = ['png', 'jpg', 'jpeg', 'webp', 'svg']
const REF_EXT = ['css', 'html', 'htm', 'txt', 'md']
// The API caps a raw upload body at 4 MiB (docs/API.md); used until the build's own quota is known (the New build page).
export const FALLBACK_MAX_BYTES = 4 * 1024 * 1024
export const ATTACH_ACCEPT = [...ASSET_EXT, ...REF_EXT].map((e) => `.${e}`).join(',')
const extOf = (n: string) => (n.includes('.') ? n.split('.').pop()!.toLowerCase() : '')

export interface Chip {
  key: number
  name: string
  size: number
  kind: 'asset' | 'reference'
  status: 'held' | 'uploading' | 'ready' | 'error'
  id: number | null
  thumb: string | null
  dims: string
  note: string
  savedNote: string
  error: string
  quota: boolean // the error is a limit the user can fix in Files
  retryable: boolean // a transient failure (busy, rate limit, network): trying again can work
  notes: string[]
  file: File | null
  cancelled: boolean
}

export function friendlyUploadError(e: unknown, fallback: string): string {
  if (!(e instanceof ApiError)) return fallback
  switch (e.status) {
    case 0: return e.message
    case 429: return 'Too many uploads for now. Wait a while and try again.'
    case 503: return 'Image processing is busy. Try again in a moment.'
    case 504: return 'Image processing took too long. Try again, or use a smaller image.'
    default: return e.message || fallback
  }
}

// Images are previewed from a data: URL (the page CSP blocks blob:).
function readThumb(f: File): Promise<string | null> {
  return new Promise((resolve) => {
    const r = new FileReader()
    r.onload = () => resolve(typeof r.result === 'string' ? r.result : null)
    r.onerror = () => resolve(null)
    r.readAsDataURL(f)
  })
}

export function useAttachments(opts: {
  buildId: () => number | null
  enabled: () => boolean // uploads on AND the composer may attach right now
  maxBytes: () => number | undefined
  onChanged: () => void
}) {
  const chips = ref<Chip[]>([])
  let seq = 0
  let queue: Promise<void> = Promise.resolve()

  const uploading = computed(() => chips.value.filter((c) => c.status === 'uploading').length)
  const rejected = computed(() => chips.value.filter((c) => c.status === 'error').length)
  // Failed uploads only (not files rejected before upload): what a retry can act on.
  const failed = computed(() => chips.value.filter((c) => c.status === 'error' && c.file))
  const held = computed(() => chips.value.filter((c) => c.status === 'held'))
  const readyIds = computed(() => chips.value.filter((c) => c.status === 'ready' && c.id !== null).map((c) => c.id as number))

  function precheck(f: File): string {
    const max = opts.maxBytes() ?? FALLBACK_MAX_BYTES
    if (f.size === 0) return 'That file is empty.'
    if (max && f.size > max) return `Too large: ${fmtSize(f.size)}, limit ${fmtSize(max)}.`
    const ext = extOf(f.name)
    if (!ASSET_EXT.includes(ext) && !REF_EXT.includes(ext)) return `${ext ? ext.toUpperCase() : 'That'} isn't supported. Use png, jpg, webp or svg for images; css, html, txt or md for references.`
    return ''
  }

  const find = (key: number) => chips.value.find((c) => c.key === key)

  async function doUpload(chip: Chip, buildId: number): Promise<boolean> {
    const live = find(chip.key)
    if (!live || !chip.file) return false
    live.status = 'uploading'
    live.error = ''
    try {
      const r = await addUpload(buildId, chip.file, chip.kind, chip.note.trim())
      if (chip.cancelled) {
        await deleteUpload(buildId, r.id).catch(() => undefined)
        opts.onChanged()
        return false
      }
      live.id = r.id
      live.status = 'ready'
      live.notes = r.notes ?? []
      live.savedNote = live.note.trim()
      live.dims = r.width && r.height ? `${r.width}×${r.height}` : ''
      live.file = null
      if (live.kind === 'asset') live.thumb = uploadContentUrl(buildId, r.id, r.version)
      opts.onChanged()
      return true
    } catch (e) {
      live.status = 'error'
      live.error = friendlyUploadError(e, "Couldn't upload the file.")
      live.retryable = e instanceof ApiError && [0, 429, 503, 504].includes(e.status)
      live.quota = e instanceof ApiError && e.status === 403 && /\b(files?|MB|limit)\b/i.test(live.error)
      return false
    }
  }

  function start(chip: Chip) {
    queue = queue.then(async () => {
      const id = opts.buildId()
      if (chip.cancelled || id === null) return
      await doUpload(chip, id)
    })
  }

  // Returns 'blocked' when nothing was queued because attaching is off.
  function addFiles(files: File[]): string {
    if (!opts.enabled()) return 'blocked'
    const holding = opts.buildId() === null
    for (const f of files) {
      const ext = extOf(f.name)
      const kind: 'asset' | 'reference' = ASSET_EXT.includes(ext) ? 'asset' : 'reference'
      const err = precheck(f)
      const chip: Chip = {
        key: ++seq, name: f.name || 'pasted file', size: f.size, kind, status: err ? 'error' : holding ? 'held' : 'uploading', id: null,
        thumb: null, dims: '', note: '', savedNote: '', error: err, quota: false, retryable: false, notes: [], file: err ? null : f, cancelled: false,
      }
      chips.value = [...chips.value, chip]
      if (err) continue
      if (holding && kind === 'asset') readThumb(f).then((t) => { const c = find(chip.key); if (c) c.thumb = t })
      if (!holding) start(find(chip.key)!)
    }
    return ''
  }

  // Held mode: upload every held (or failed-but-retryable) file to the new build, in order. Returns the failures.
  async function uploadHeld(buildId: number): Promise<Chip[]> {
    const todo = chips.value.filter((c) => (c.status === 'held' || c.status === 'error') && c.file)
    for (const c of todo) await doUpload(c, buildId)
    return chips.value.filter((c) => c.status === 'error' && c.file)
  }

  async function remove(key: number) {
    const c = find(key)
    if (!c) return
    c.cancelled = true
    chips.value = chips.value.filter((x) => x.key !== key)
    const bid = opts.buildId()
    if (c.id !== null && bid !== null) {
      try {
        await deleteUpload(bid, c.id)
        opts.onChanged()
      } catch {
        // already gone or blocked by a run; the file stays listed in Files
      }
    }
  }

  function retry(key: number) {
    const c = find(key)
    if (!c || !c.file) return
    if (opts.buildId() === null) {
      c.status = 'held'
      c.error = ''
      return
    }
    c.status = 'uploading'
    c.error = ''
    start(c)
  }

  function setNote(key: number, v: string) {
    const c = find(key)
    if (c) c.note = v
  }

  async function saveNote(key: number) {
    const c = find(key)
    const bid = opts.buildId()
    if (!c || c.id === null || bid === null || c.note.trim() === c.savedNote) return
    try {
      await describeUpload(bid, c.id, c.note.trim())
      c.savedNote = c.note.trim()
    } catch (e) {
      c.error = friendlyUploadError(e, "Couldn't save the note.")
    }
  }

  // Unsent files already on the build (run_id null): show them as chips, drop chips whose file is gone.
  function hydrate(unsent: KaisenUpload[]) {
    const bid = opts.buildId()
    if (bid === null) return
    const ids = new Set(unsent.map((u) => u.id))
    chips.value = chips.value.filter((c) => c.id === null || ids.has(c.id))
    for (const u of unsent) {
      if (chips.value.some((c) => c.id === u.id)) continue
      chips.value = [...chips.value, {
        key: ++seq, name: u.name_orig, size: u.size, kind: u.kind, status: 'ready', id: u.id,
        thumb: u.kind === 'asset' ? uploadContentUrl(bid, u.id, u.version) : null,
        dims: u.width && u.height ? `${u.width}×${u.height}` : '', note: u.description, savedNote: u.description,
        error: '', quota: false, retryable: false, notes: [], file: null, cancelled: false,
      }]
    }
  }

  // After Send: the files stay in the build (they are bound to the new run); only the composer's chips go.
  function clear() {
    chips.value = []
  }

  return { chips, uploading, rejected, failed, held, readyIds, addFiles, uploadHeld, remove, retry, setNote, saveNote, hydrate, clear }
}
