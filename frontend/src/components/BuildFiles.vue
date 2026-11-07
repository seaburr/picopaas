<script setup lang="ts">
import { ref, watch } from 'vue'
import CopyButton from './CopyButton.vue'
import IconButton from './IconButton.vue'
import { getBuildFile } from '../api/kaisen'
import { ApiError } from '../api/client'
import type { KaisenFile } from '../types'

const props = defineProps<{ buildId: number; files: { path: string; size: number }[] }>()

const selected = ref('')
const file = ref<KaisenFile | null>(null)
const loading = ref(false)
const error = ref('')

const size = (n: number) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`)

async function open(path: string) {
  selected.value = path
  file.value = null
  error.value = ''
  loading.value = true
  try {
    const f = await getBuildFile(props.buildId, path)
    if (selected.value === path) file.value = f
  } catch (e) {
    if (selected.value === path) error.value = e instanceof ApiError ? e.message : "Couldn't load the file."
  } finally {
    if (selected.value === path) loading.value = false
  }
}
watch(() => props.buildId, () => { selected.value = ''; file.value = null; error.value = '' })
</script>

<template>
  <details class="disc sec py-1.5" aria-labelledby="h-files">
    <summary><h2 id="h-files" class="section-title">Files <span class="mono font-normal text-zinc-400">{{ files.length }}</span></h2></summary>
    <div class="pb-2 pt-1">
      <p v-if="files.length === 0" class="text-sm text-zinc-300">No files yet. They appear as soon as Kaisen writes them.</p>
      <div v-else class="space-y-2">
        <ul class="max-h-56 overflow-y-auto">
          <li v-for="f in files" :key="f.path" class="flex items-center gap-2 border-b border-pico-border">
            <button type="button" class="mono min-h-[28px] min-w-0 flex-1 truncate py-0.5 text-left hover:underline max-md:min-h-[44px]" :aria-pressed="selected === f.path" :class="selected === f.path ? 'font-semibold text-white' : 'text-zinc-300'" :title="f.path" @click="open(f.path)">{{ f.path }}</button>
            <span class="mono shrink-0 text-zinc-400">{{ size(f.size) }}</span>
          </li>
        </ul>
        <div class="min-w-0" aria-live="polite">
          <p v-if="!selected" class="help">Select a file to read it.</p>
          <template v-else>
            <div class="flex items-center justify-between gap-2">
              <span class="mono break-all">{{ selected }}</span>
              <CopyButton v-if="file && file.content" :text="file.content" label="Copy file" sm />
            </div>
            <div v-if="loading" class="mt-2 h-24 animate-pulse rounded-sm bg-pico-card" aria-busy="true" aria-label="Loading file" />
            <div v-else-if="error" role="alert" class="alert-error mt-2 flex flex-wrap items-center justify-between gap-2">
              <span>{{ error }}</span>
              <IconButton label="Retry" @click="open(selected)" />
            </div>
            <p v-else-if="file && (file.binary || file.truncated || file.content === null)" class="help">This file is too large to preview. Download the code to see it.</p>
            <pre v-else-if="file" class="mono mt-2 max-h-96 overflow-auto rounded-md bg-pico-bg p-3 text-zinc-200" tabindex="0" :aria-label="`Contents of ${selected}`">{{ file.content }}</pre>
          </template>
        </div>
      </div>
    </div>
  </details>
</template>
