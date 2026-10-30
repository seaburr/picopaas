<script setup lang="ts">
import { ref, watch } from 'vue'
import { RotateCw } from 'lucide-vue-next'
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
  <section class="card card-pad" aria-labelledby="h-files">
    <h2 id="h-files" class="section-title mb-2">Files ({{ files.length }})</h2>
    <p v-if="files.length === 0" class="text-sm text-zinc-300">No files yet. They appear as soon as Kaisen writes them.</p>
    <div v-else class="grid gap-3 md:grid-cols-3">
      <ul class="space-y-1 md:col-span-1">
        <li v-for="f in files" :key="f.path" class="flex items-center gap-2">
          <button type="button" class="btn-ghost btn-sm mono min-w-0 flex-1 justify-start truncate" :aria-pressed="selected === f.path" :class="selected === f.path ? 'bg-pico-card' : ''" @click="open(f.path)">
            <span class="truncate">{{ f.path }}</span>
          </button>
          <span class="meta shrink-0">{{ size(f.size) }}</span>
        </li>
      </ul>
      <div class="min-w-0 md:col-span-2" aria-live="polite">
        <p v-if="!selected" class="help">Select a file to read it.</p>
        <template v-else>
          <div class="flex items-center justify-between gap-2">
            <span class="mono break-all text-sm">{{ selected }}</span>
            <CopyButton v-if="file && file.content" :text="file.content" label="Copy file" sm />
          </div>
          <div v-if="loading" class="mt-2 h-24 animate-pulse rounded-sm bg-pico-card" aria-busy="true" aria-label="Loading file" />
          <div v-else-if="error" role="alert" class="alert-error mt-2 flex flex-wrap items-center justify-between gap-2">
            <span>{{ error }}</span>
            <IconButton :icon="RotateCw" label="Retry" @click="open(selected)" />
          </div>
          <p v-else-if="file && (file.binary || file.truncated || file.content === null)" class="help">This file is too large to preview. Download the code to see it.</p>
          <pre v-else-if="file" class="mono mt-2 max-h-96 overflow-auto rounded-md bg-pico-bg p-3 text-zinc-200" tabindex="0" :aria-label="`Contents of ${selected}`">{{ file.content }}</pre>
        </template>
      </div>
    </div>
  </section>
</template>
