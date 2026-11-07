<script setup lang="ts">
import { computed, ref } from 'vue'
import CodeReader, { type ReaderContent } from './CodeReader.vue'
import { getBuildFile } from '../api/kaisen'
import { fmtSize } from '../composables/kaisen'

const props = defineProps<{ buildId: number; files: { path: string; size: number }[] }>()
const reading = ref(false)
const total = computed(() => props.files.reduce((n, f) => n + f.size, 0))

async function load(path: string): Promise<ReaderContent> {
  const f = await getBuildFile(props.buildId, path)
  if (f.binary || f.truncated || f.content === null) return { text: null, note: 'This file is too large to preview. Download the code to see it.' }
  return { text: f.content }
}
</script>

<template>
  <details class="disc sec py-1.5" aria-labelledby="h-files">
    <summary><h2 id="h-files" class="section-title">Project files <span class="mono font-normal text-zinc-400">{{ files.length }}</span></h2></summary>
    <div class="pb-2 pt-1">
      <p v-if="files.length === 0" class="text-sm text-zinc-300">No project files yet. They appear as soon as Kaisen writes them.</p>
      <template v-else>
        <p class="meta mb-2"><span class="mono">{{ files.length }}</span> files, <span class="mono">{{ fmtSize(total) }}</span> in the code Kaisen wrote.</p>
        <button type="button" class="btn-secondary btn-sm" @click="reading = true">Read project files</button>
      </template>
    </div>
  </details>
  <CodeReader :open="reading" title="Project files" :items="files" :load="load" @close="reading = false" />
</template>
