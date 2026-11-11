<script setup lang="ts">
import { computed, ref } from 'vue'

// Live preview of the build's app in a sandboxed frame. The app is on a sibling subdomain of the platform, so the frame
// gets NO allow-same-origin (scripts run in an opaque origin and can't touch platform cookies or storage) and no
// referrer. The platform CSP only allows frame-src for https://*.<apps domain>.
const props = defineProps<{ hostname: string; status: string }>()

const PRESETS = [
  { key: 'phone', label: 'Phone', width: '390px' },
  { key: 'tablet', label: 'Tablet', width: '768px' },
  { key: 'full', label: 'Desktop', width: '100%' },
] as const
type Key = (typeof PRESETS)[number]['key']

const wide = typeof window !== 'undefined' && window.matchMedia('(min-width: 1024px)').matches
const open = ref(wide) // collapsed by default on a phone
const preset = ref<Key>('full')
const nonce = ref(0) // bumping it remounts the iframe, which reloads it
const loaded = ref(false)
const slow = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined

const url = computed(() => `https://${props.hostname}/`)
// Never frame the platform's own origin.
const safe = computed(() => props.hostname.toLowerCase() !== window.location.hostname.toLowerCase())
const ready = computed(() => props.status === 'running' && safe.value)
const width = computed(() => PRESETS.find((p) => p.key === preset.value)?.width ?? '100%')

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
        <div class="flex gap-1" role="group" aria-label="Preview width">
          <button v-for="p in PRESETS" :key="p.key" type="button" class="btn-secondary btn-sm" :aria-pressed="preset === p.key" :class="preset === p.key ? 'cur' : ''" @click="preset = p.key">{{ p.label }}</button>
        </div>
        <button type="button" class="btn-secondary btn-sm" :disabled="!ready" @click="reload">Reload</button>
        <a :href="url" target="_blank" rel="noopener noreferrer" class="link text-sm">Open in new tab<span class="sr-only"> (opens in new tab)</span></a>
      </div>
      <template v-if="open">
        <p v-if="!ready" class="help mt-2" role="status" data-testid="preview-fallback">The app is not reachable yet, so there is nothing to show here. Use Open in new tab once it is running.</p>
        <template v-else>
          <p class="help mt-2" data-testid="preview-host">Shown below: <strong class="font-mono">{{ hostname }}</strong>. This is the app's own page, not the platform. Never enter your platform password in it.</p>
          <div class="mt-2 max-w-full overflow-x-auto rounded-md border border-pico-border">
            <iframe
              :key="nonce" :src="url" title="Live preview of the app" sandbox="allow-scripts allow-forms allow-popups" referrerpolicy="no-referrer"
              class="block h-96 max-w-none bg-white" :style="{ width }" data-testid="preview-frame" @load="onLoad"
            />
          </div>
          <p v-if="slow && !loaded" class="help mt-2" role="status" data-testid="preview-fallback">The preview did not load. The app may still be starting or may not allow being framed. Use Open in new tab.</p>
          <p v-else class="help mt-2">Blank? The app may not allow being framed. Use Open in new tab.</p>
        </template>
      </template>
    </div>
  </details>
</template>
