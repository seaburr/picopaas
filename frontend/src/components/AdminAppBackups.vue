<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { adminBackupSets, adminDownloadBackupSet, adminGetBackupExport } from '../api/admin'
import { ApiError } from '../api/client'
import { stamp, stampTitle } from '../utils/format'
import { useToastStore } from '../stores/toast'
import type { AdminBackupSets } from '../types'

// Admin: one app's backups (deleted apps too) with a package download. Opened from Health > Backups.
const props = defineProps<{ appId: number }>()
const toast = useToastStore()
const data = ref<AdminBackupSets | null>(null)
const error = ref('')
const busy = ref<Set<number>>(new Set())
let stopped = false

const MB = 1048576
const size = (n: number) => (n >= MB ? `${(n / MB).toFixed(1)} MB` : n > 0 ? `${Math.max(1, Math.round(n / 1024))} KB` : '0')
const msg = (e: unknown, f: string) => (e instanceof ApiError ? e.message : f)

async function load() {
  try {
    data.value = await adminBackupSets(props.appId)
    error.value = ''
  } catch (e) {
    error.value = msg(e, "Couldn't load this app's backups.")
  }
}
onMounted(load)
onBeforeUnmount(() => { stopped = true })

function openUrl(url: string, filename: string | null) {
  const a = document.createElement('a')
  a.href = url
  if (filename) a.download = filename
  a.rel = 'noopener noreferrer'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))

// Start (or join) the export, poll until ready, then open the fresh link.
async function download(setId: number) {
  if (busy.value.has(setId)) return
  busy.value = new Set(busy.value).add(setId)
  try {
    let ex = await adminDownloadBackupSet(props.appId, setId)
    for (let i = 0; i < 180 && !stopped && (ex.status === 'queued' || ex.status === 'running'); i++) {
      await wait(5000)
      ex = await adminGetBackupExport(props.appId, ex.export_id)
    }
    if (ex.status === 'ready' && !ex.url) ex = await adminGetBackupExport(props.appId, ex.export_id)
    if (ex.status === 'ready' && ex.url) openUrl(ex.url, ex.filename)
    else if (ex.status === 'failed') toast.error(`Download failed: ${ex.error || 'unknown error'}`)
    else if (!stopped) toast.error('The download is not ready yet. Try again in a minute.')
  } catch (e) {
    toast.error(msg(e, "Couldn't prepare the download."))
  } finally {
    const n = new Set(busy.value)
    n.delete(setId)
    busy.value = n
  }
}
</script>

<template>
  <div class="rounded-md bg-pico-card p-3 text-xs" aria-live="polite">
    <div v-if="error" role="alert" class="alert-error">{{ error }}</div>
    <p v-else-if="!data" class="text-zinc-400" role="status">Loading backups…</p>
    <template v-else>
      <p class="mb-2 text-zinc-400">
        {{ data.app_name }}<template v-if="data.deleted"> (deleted<template v-if="data.purge_after">, purged after {{ stamp(data.purge_after) }}</template>)</template>.
        Downloads are one .tar.gz per backup, limited to 3 per app per hour, and are audited.
      </p>
      <p v-if="data.purged" class="text-amber-300">This app's backups have been purged.</p>
      <p v-else-if="!data.backups.length" class="text-zinc-400">No backups.</p>
      <table v-else class="tbl min-w-[32rem]">
        <thead><tr><th scope="col">Taken</th><th scope="col">Trigger</th><th scope="col">Status</th><th scope="col">Parts</th><th scope="col" class="text-right">Size</th><th scope="col"><span class="sr-only">Download</span></th></tr></thead>
        <tbody class="text-zinc-300">
          <tr v-for="b in data.backups" :key="b.id">
            <td class="mono whitespace-nowrap" :title="stampTitle(b.created_at)">{{ stamp(b.created_at) }}</td>
            <td>{{ b.trigger }}</td>
            <td>{{ b.status }}</td>
            <td>{{ b.parts.map((p) => p.kind).join(', ') }}</td>
            <td class="mono text-right">{{ size(b.size_bytes) }}</td>
            <td class="text-right">
              <button type="button" class="btn-secondary btn-sm" :disabled="busy.has(b.id) || b.status === 'running' || b.status === 'failed' || b.status === 'deleting'" @click="download(b.id)">
                {{ busy.has(b.id) ? 'Preparing…' : 'Download' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>
