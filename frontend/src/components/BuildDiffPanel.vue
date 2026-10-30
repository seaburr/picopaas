<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ChevronRight, RotateCw } from 'lucide-vue-next'
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

// Plain text lines only, never HTML. Colour by first character.
const lineCls = (l: string) =>
  l.startsWith('+') && !l.startsWith('+++') ? 'text-status-running'
  : l.startsWith('-') && !l.startsWith('---') ? 'text-danger-light'
  : l.startsWith('@@') ? 'text-accent-light' : 'text-zinc-300'
const lines = (d: string) => d.replace(/\n$/, '').split('\n')
</script>

<template>
  <div class="mt-2 space-y-2 rounded-md border border-pico-border bg-pico-bg p-3" aria-live="polite">
    <p v-if="title" class="text-sm font-medium text-zinc-200">{{ title }}</p>
    <p v-if="loading" class="text-sm text-zinc-300 motion-safe:animate-pulse">Loading changes…</p>
    <div v-else-if="error" class="alert-error flex items-center justify-between gap-2">
      <span>{{ error }}</span>
      <IconButton :icon="RotateCw" label="Retry" sm @click="fetchDiff" />
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
        <details v-for="c in diff.changed" :key="c.path" class="group rounded-md border border-pico-border">
          <summary class="flex cursor-pointer items-center gap-1 px-2 py-1 text-sm">
            <ChevronRight :size="14" class="shrink-0 transition-transform group-open:rotate-90" aria-hidden="true" />
            <span class="mono break-all">{{ c.path }}</span>
          </summary>
          <p v-if="!c.diff" class="border-t border-pico-border px-2 py-1 text-sm text-zinc-300">Diff not shown (too large).</p>
          <pre v-else class="mono max-h-96 overflow-auto border-t border-pico-border p-2 text-xs" tabindex="0" :aria-label="`Diff of ${c.path}`"><template v-for="(l, i) in lines(c.diff)" :key="i"><span :class="['block w-max min-w-full', lineCls(l)]">{{ l || ' ' }}</span></template></pre>
        </details>
      </div>
      <p v-if="diff.truncated" class="help">Too many changes to show them all. Files marked "Diff not shown" are listed without their changes.</p>
    </template>
  </div>
</template>
