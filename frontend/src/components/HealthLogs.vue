<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { Download, RefreshCw } from 'lucide-vue-next'
import IconButton from './IconButton.vue'
import { downloadPlatformLogs, getPlatformLogs, getPlatformPods, type LogQuery } from '../api/admin'
import { ApiError } from '../api/client'
import { fmtTime } from '../utils/tz'
import { useToastStore } from '../stores/toast'
import type { PlatformPod } from '../types'

const toast = useToastStore()
const pods = ref<PlatformPod[]>([])
const podsError = ref('')
const ns = ref('')
const podName = ref('')
const container = ref('')
const tail = ref(200)
const since = ref(0)
const previous = ref(false)
const lines = ref<string[]>([])
const truncated = ref(false)
const loading = ref(false)
const dl = ref(false)
const error = ref('')
const fetched = ref<Date | null>(null)
const viewer = ref<HTMLElement | null>(null)

const TAILS = [100, 200, 500, 1000, 2000]
const SINCES = [{ v: 0, l: 'any time' }, { v: 900, l: '15 min' }, { v: 3600, l: '1 hour' }, { v: 21600, l: '6 hours' }, { v: 86400, l: '24 hours' }, { v: 604800, l: '7 days' }]

const namespaces = computed(() => [...new Set(pods.value.map((p) => p.namespace))].sort())
const podsIn = computed(() => pods.value.filter((p) => p.namespace === ns.value))
const containers = computed(() => pods.value.find((p) => p.namespace === ns.value && p.name === podName.value)?.containers ?? [])
const query = (): LogQuery => ({ namespace: ns.value, pod: podName.value, container: container.value, tail: tail.value, since_seconds: since.value || null, previous: previous.value })
const ready = computed(() => !!ns.value && !!podName.value && !!container.value)

async function loadPods() {
  try {
    pods.value = (await getPlatformPods()).pods
    podsError.value = ''
    if (!ns.value && namespaces.value.length) ns.value = namespaces.value.includes('picopaas-system') ? 'picopaas-system' : namespaces.value[0]
  } catch (e) {
    podsError.value = e instanceof ApiError ? e.message : "Couldn't list platform pods."
  }
}
onMounted(loadPods)
watch(ns, () => {
  if (!podsIn.value.some((p) => p.name === podName.value)) podName.value = podsIn.value[0]?.name ?? ''
})
watch(podName, () => {
  if (!containers.value.includes(container.value)) container.value = containers.value[0] ?? ''
})
watch([container, tail, since, previous], () => {
  if (ready.value) fetchLogs()
})
watch(container, () => {
  lines.value = []
})

async function fetchLogs() {
  if (!ready.value) return
  loading.value = true
  try {
    const r = await getPlatformLogs(query())
    lines.value = r.lines
    truncated.value = r.truncated
    error.value = ''
    fetched.value = new Date()
    await nextTick()
    if (viewer.value) viewer.value.scrollTop = viewer.value.scrollHeight
  } catch (e) {
    lines.value = []
    error.value = e instanceof ApiError ? e.message : "Couldn't read those logs."
  } finally {
    loading.value = false
  }
}
async function download() {
  dl.value = true
  try {
    await downloadPlatformLogs({ ...query(), tail: 10000 })
  } catch (e) {
    toast.error(`Couldn't download the logs: ${e instanceof ApiError ? e.message : 'try again'}`)
  } finally {
    dl.value = false
  }
}
</script>

<template>
  <section id="sec-logs" class="scroll-mt-4" aria-label="Platform logs">
    <h2 class="mb-1.5 text-sm font-semibold text-zinc-300">Platform logs</h2>
    <div v-if="podsError" role="alert" class="alert-error">{{ podsError }}</div>
    <div v-else class="min-w-0 rounded-md border border-pico-border bg-pico-panel p-3">
      <div class="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <div>
          <label for="pl-ns" class="label">Namespace</label>
          <select id="pl-ns" v-model="ns" class="input"><option v-for="n in namespaces" :key="n" :value="n">{{ n }}</option></select>
        </div>
        <div class="col-span-2 sm:col-span-1 lg:col-span-2">
          <label for="pl-pod" class="label">Pod</label>
          <select id="pl-pod" v-model="podName" class="input mono"><option v-for="p in podsIn" :key="p.name" :value="p.name">{{ p.name }}</option></select>
        </div>
        <div>
          <label for="pl-ctr" class="label">Container</label>
          <select id="pl-ctr" v-model="container" class="input"><option v-for="c in containers" :key="c" :value="c">{{ c }}</option></select>
        </div>
        <div>
          <label for="pl-tail" class="label">Last lines</label>
          <select id="pl-tail" v-model.number="tail" class="input"><option v-for="t in TAILS" :key="t" :value="t">{{ t }}</option></select>
        </div>
        <div>
          <label for="pl-since" class="label">Since</label>
          <select id="pl-since" v-model.number="since" class="input"><option v-for="s in SINCES" :key="s.v" :value="s.v">{{ s.l }}</option></select>
        </div>
      </div>
      <div class="mt-2 flex flex-wrap items-center gap-3">
        <label class="flex items-center gap-2 text-sm text-zinc-200"><input v-model="previous" type="checkbox" class="check" /> <span>Previous container (before the last restart)</span></label>
        <span class="flex-1" />
        <span v-if="fetched" class="text-xs text-zinc-400">Fetched {{ fmtTime(fetched) }}</span>
        <IconButton :icon="RefreshCw" :label="loading ? 'Loading logs…' : 'Refresh logs'" :disabled="!ready" :busy="loading" @click="fetchLogs" />
        <IconButton :icon="Download" :label="dl ? 'Preparing download…' : 'Download logs'" :disabled="!ready" :busy="dl" @click="download" />
      </div>
      <p class="mt-1 text-xs text-zinc-400">Download includes up to the last 10,000 lines. Downloads are recorded in the audit log.</p>
      <div v-if="error" role="alert" class="alert-error mt-2">{{ error }}</div>
      <p v-if="truncated" class="mt-2 text-xs text-amber-400" role="status">Showing the newest lines only; the output was cut to fit. Use Download for more.</p>
      <pre
        ref="viewer" tabindex="0" aria-label="Log output"
        class="mono mt-2 h-80 overflow-auto whitespace-pre rounded border border-pico-border bg-black/40 p-2 text-xs leading-4 text-zinc-200"
      >{{ lines.length ? lines.join('\n') : (ready ? (loading ? '' : (fetched ? 'No log lines.' : 'Press Refresh to load logs.')) : 'Pick a namespace, pod and container.') }}</pre>
    </div>
  </section>
</template>
