<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import CodeReader, { type ReaderContent, type ReaderItem } from './CodeReader.vue'
import IconButton from './IconButton.vue'
import { ApiError } from '../api/client'
import type { KaisenDiff } from '../types'

const props = defineProps<{ load: () => Promise<KaisenDiff>; title?: string }>()

const diff = ref<KaisenDiff | null>(null)
const error = ref('')
const loading = ref(false)

async function fetchDiff() {
  loading.value = true
  error.value = ''
  try {
    diff.value = await props.load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load the changes."
  } finally {
    loading.value = false
  }
}
onMounted(fetchDiff)

const reading = ref(false)
const start = ref('')
const items = computed<ReaderItem[]>(() => !diff.value ? [] : [
  ...diff.value.changed.map((c) => ({ path: c.path, tag: 'changed' })),
  ...diff.value.added.map((p) => ({ path: p, tag: 'added' })),
  ...diff.value.removed.map((p) => ({ path: p, tag: 'removed' })),
])
// Plain text only, never HTML. Added and removed files have no diff body, so say so.
async function loadOne(path: string): Promise<ReaderContent> {
  const d = diff.value
  if (!d) return { text: null }
  const c = d.changed.find((x) => x.path === path)
  if (c) return c.diff ? { text: c.diff } : { text: null, note: 'Diff not shown (too large).' }
  return { text: null, note: d.added.includes(path) ? 'This file was added. Open it in Project files to read it.' : 'This file was removed.' }
}
function read(path: string) { start.value = path; reading.value = true }
</script>

<template>
  <div class="mt-2 space-y-2 rounded-md border border-pico-border bg-pico-bg p-3" aria-live="polite">
    <p v-if="title" class="text-sm font-medium text-zinc-200">{{ title }}</p>
    <p v-if="loading" class="text-sm text-zinc-300 motion-safe:animate-pulse">Loading changes…</p>
    <div v-else-if="error" class="alert-error flex items-center justify-between gap-2">
      <span>{{ error }}</span>
      <IconButton label="Retry" sm @click="fetchDiff" />
    </div>
    <template v-else-if="diff">
      <p class="text-sm text-zinc-200">
        <span class="mono">{{ diff.added.length }}</span> added,
        <span class="mono">{{ diff.removed.length }}</span> removed,
        <span class="mono">{{ diff.changed.length }}</span> changed
      </p>
      <p v-if="!diff.added.length && !diff.removed.length && !diff.changed.length" class="text-sm text-zinc-300">No differences.</p>
      <div v-if="diff.added.length" class="text-sm">
        <p class="meta">Added</p>
        <ul class="mono space-y-0.5"><li v-for="f in diff.added" :key="f" class="break-all text-status-running">+ {{ f }}</li></ul>
      </div>
      <div v-if="diff.removed.length" class="text-sm">
        <p class="meta">Removed</p>
        <ul class="mono space-y-0.5"><li v-for="f in diff.removed" :key="f" class="break-all text-danger-light">- {{ f }}</li></ul>
      </div>
      <div v-if="diff.changed.length" class="space-y-1">
        <p class="meta">Changed</p>
        <ul class="divide-y divide-pico-border border-y border-pico-border">
          <li v-for="c in diff.changed" :key="c.path" class="flex items-center gap-2 py-0.5">
            <span class="mono min-w-0 flex-1 break-all text-sm">{{ c.path }}</span>
            <span v-if="!c.diff" class="meta shrink-0">too large</span>
            <button type="button" class="row-act shrink-0" :aria-label="`Read diff of ${c.path}`" @click="read(c.path)">Read diff</button>
          </li>
        </ul>
      </div>
      <p v-if="diff.truncated" class="help">Too many changes to show them all. Files marked "too large" are listed without their changes.</p>
    </template>
    <CodeReader :open="reading" :title="title || 'Changes'" :items="items" :load="loadOne" diff :initial-path="start" @close="reading = false" />
  </div>
</template>
