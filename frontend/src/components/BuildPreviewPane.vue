<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

// Live preview of the build's app in a sandboxed frame. The app is on a sibling subdomain of the platform, so the frame
// gets NO allow-same-origin (scripts run in an opaque origin and can't touch platform cookies or storage) and no
// referrer. The platform CSP only allows frame-src for https://*.<apps domain>.
const props = defineProps<{ hostname: string; status: string }>()

const PRESETS = [
  { key: 'phone', label: 'Phone', w: 390, h: 700 },
  { key: 'tablet', label: 'Tablet', w: 768, h: 1024 },
  { key: 'full', label: 'Desktop', w: 1280, h: 800 },
] as const
type Key = (typeof PRESETS)[number]['key']

const wide = typeof window !== 'undefined' && window.matchMedia('(min-width: 1024px)').matches
const open = ref(wide) // collapsed by default on a phone
const preset = ref<Key>('phone') // the pane is narrow wherever it sits; Phone is the size that stays readable
const nonce = ref(0) // bumping it remounts the iframe, which reloads it
const loaded = ref(false)
const slow = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined

const url = computed(() => `https://${props.hostname}/`)
// Never frame the platform's own origin.
const safe = computed(() => props.hostname.toLowerCase() !== window.location.hostname.toLowerCase())
const ready = computed(() => props.status === 'running' && safe.value)
// The frame is laid out at the preset's real size and scaled down to fit the pane, so "Tablet" in a narrow side panel
// still renders the app at 768px instead of scrolling sideways.
const size = computed(() => PRESETS.find((p) => p.key === preset.value) ?? PRESETS[2])
const box = ref<HTMLElement | null>(null)
const boxW = ref(0)
const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver((es) => { boxW.value = es[0].contentRect.width }) : null
watch(box, (el, old) => {
  if (old) ro?.unobserve(old)
  if (el) {
    boxW.value = el.clientWidth
    ro?.observe(el)
  }
})
onBeforeUnmount(() => ro?.disconnect())
const scale = computed(() => (boxW.value > 0 ? Math.min(1, boxW.value / size.value.w) : 1))
const pct = computed(() => Math.round(scale.value * 100))

function arm() {
  loaded.value = false
  slow.value = false
  clearTimeout(timer)
  timer = setTimeout(() => { if (!loaded.value) slow.value = true }, 10000)
}
function onToggle(e: Event) {
  open.value = (e.target as HTMLDetailsElement).open
  if (open.value) arm()
}
function reload() {
  nonce.value++
  arm()
}
function onLoad() {
  loaded.value = true
  clearTimeout(timer)
}
if (open.value) arm()
</script>

<template>
  <details class="disc mt-3 border-t border-pico-border pt-2" :open="open" @toggle="onToggle">
    <summary class="text-sm font-medium max-lg:min-h-[44px]">Preview pane</summary>
    <div class="pt-2">
      <div class="flex flex-wrap items-center gap-2">
        <div class="flex gap-1" role="group" aria-label="Preview width" data-testid="preview-sizes">
          <button v-for="p in PRESETS" :key="p.key" type="button" class="btn-secondary btn-sm aria-pressed:border-accent-light aria-pressed:bg-pico-card aria-pressed:font-semibold aria-pressed:text-white" :aria-pressed="preset === p.key" @click="preset = p.key">{{ p.label }}</button>
        </div>
        <button type="button" class="btn-secondary btn-sm" :disabled="!ready" @click="reload">Reload</button>
        <a :href="url" target="_blank" rel="noopener noreferrer" class="link text-sm">Open in new tab<span class="sr-only"> (opens in new tab)</span></a>
      </div>
      <template v-if="open">
        <p v-if="!ready" class="help mt-2" role="status" data-testid="preview-fallback">The app is not reachable yet, so there is nothing to show here. Use Open in new tab once it is running.</p>
        <template v-else>
          <p class="help mt-2" data-testid="preview-host">Shown below: <strong class="font-mono">{{ hostname }}</strong>. This is the app's own page, not the platform. Never enter your platform password in it.</p>
          <div ref="box" class="mt-2 overflow-hidden rounded-md border border-pico-border" :style="{ height: `${Math.round(size.h * scale)}px` }">
            <iframe
              :key="nonce" :src="url" title="Live preview of the app" sandbox="allow-scripts allow-forms allow-popups" referrerpolicy="no-referrer"
              class="block max-w-none origin-top-left bg-white" :style="{ width: `${size.w}px`, height: `${size.h}px`, transform: `scale(${scale})` }" data-testid="preview-frame" @load="onLoad"
            />
          </div>
          <p class="meta mt-1" data-testid="preview-scale">{{ size.w }} × {{ size.h }} px<template v-if="pct < 100">, shown at {{ pct }}%</template></p>
          <p v-if="slow && !loaded" class="help mt-2" role="status" data-testid="preview-fallback">The preview did not load. The app may still be starting or may not allow being framed. Use Open in new tab.</p>
          <p v-else class="help mt-2">Showing a blank page? The app may not allow being framed. Use Open in new tab.</p>
        </template>
      </template>
    </div>
  </details>
</template>
