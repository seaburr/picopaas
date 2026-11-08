<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

// An app name in the sidebar tree, plus its Kaisen mark. The mark is never dropped: the full "Kaisen" tag shows when
// the name still fits beside it, otherwise a compact "K" tag. Decided by measuring the row (fonts, widths, the
// full-screen phone menu), not by counting characters.
const props = defineProps<{ name: string; kaisen?: boolean }>()
const nm = ref<HTMLElement | null>(null)
const compact = ref(false)

// Sub-pixel exact: scrollWidth rounds, so a name 0.4px too wide would read as fitting yet still show the ellipsis.
function clipped(el: HTMLElement) {
  const r = document.createRange()
  r.selectNodeContents(el)
  return r.getBoundingClientRect().width > el.getBoundingClientRect().width + 0.01
}

async function measure() {
  if (!props.kaisen || !nm.value) return
  compact.value = false
  await nextTick()
  const el = nm.value
  if (el) compact.value = clipped(el)
}

let ro: ResizeObserver | null = null
onMounted(() => {
  measure()
  document.fonts?.ready.then(measure).catch(() => {})
  const row = nm.value?.parentElement
  if (row && typeof ResizeObserver !== 'undefined') {
    let w = row.clientWidth
    ro = new ResizeObserver(() => {
      if (row.clientWidth !== w) { w = row.clientWidth; measure() }
    })
    ro.observe(row)
  }
})
onBeforeUnmount(() => ro?.disconnect())
watch(() => [props.name, props.kaisen], measure)
</script>

<template>
  <span ref="nm" class="m mono nm">{{ name }}</span>
  <span v-if="kaisen" class="tag ktag" :data-compact="compact || undefined" title="Built with Kaisen (Preview)"><span aria-hidden="true">{{ compact ? 'K' : 'Kaisen' }}</span><span class="sr-only">{{ compact ? ' Kaisen' : '' }} (built with Kaisen)</span></span>
</template>
