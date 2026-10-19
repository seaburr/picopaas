<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import NavBar from '../components/NavBar.vue'
import StatusBadge from '../components/StatusBadge.vue'
import DbCapacityBar from '../components/DbCapacityBar.vue'
import { getHealth } from '../api/admin'
import { ApiError } from '../api/client'
import type { ClusterHealth, HealthApp } from '../types'

const data = ref<ClusterHealth | null>(null)
const error = ref('')
const loading = ref(false)
const updated = ref<Date | null>(null)
let timer: number | undefined

async function load() {
  loading.value = true
  try {
    data.value = await getHealth()
    updated.value = new Date()
    error.value = ''
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Failed to load health'
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  load()
  timer = window.setInterval(load, 15000)
})
onBeforeUnmount(() => window.clearInterval(timer))

const pct = (used: number | null, limit: number) => (used === null || !limit ? null : (used / limit) * 100)
const w = (v: number | null, max: number) => Math.min(100, max ? ((v ?? 0) / max) * 100 : 0) + '%'
const hot = (v: number | null) => v !== null && v > 80
function age(s: number) {
  if (s >= 86400) return Math.floor(s / 86400) + 'd'
  if (s >= 3600) return Math.floor(s / 3600) + 'h'
  return Math.floor(s / 60) + 'm'
}
const num = (v: number | null, unit: string) => (v === null ? 'n/a' : `${v}${unit}`)
const NA = 'metrics unavailable'
const fmtUse = (used: number | null, lim: number, unit: string) =>
  `${used}${unit} / ${lim}${unit} (${Math.round(((used ?? 0) / (lim || 1)) * 100)}%)`
function pressure(n: { cpu_requested_m: number; cpu_allocatable_m: number; mem_requested_mi: number; mem_allocatable_mi: number }) {
  const c = n.cpu_allocatable_m ? (n.cpu_requested_m / n.cpu_allocatable_m) * 100 : 0
  const m = n.mem_allocatable_mi ? (n.mem_requested_mi / n.mem_allocatable_mi) * 100 : 0
  return { c: Math.round(c), m: Math.round(m), tight: c >= 85 || m >= 85 }
}

type SortKey = 'cpu' | 'mem'
const sortKey = ref<SortKey>('cpu')
const sortDesc = ref(true)
function sortBy(k: SortKey) {
  if (sortKey.value === k) sortDesc.value = !sortDesc.value
  else {
    sortKey.value = k
    sortDesc.value = true
  }
}
const usage = (a: HealthApp, k: SortKey) => (k === 'cpu' ? pct(a.cpu_used_m, a.cpu_limit_m) : pct(a.mem_used_mi, a.mem_limit_mi))
const sortedApps = computed(() => {
  const k = sortKey.value
  const dir = sortDesc.value ? -1 : 1
  return [...(data.value?.apps ?? [])].sort((a, b) => {
    const x = usage(a, k)
    const y = usage(b, k)
    if (x === null && y === null) return a.name.localeCompare(b.name)
    if (x === null) return 1 // unknown always last
    if (y === null) return -1
    return (x - y) * dir
  })
})
const ariaSort = (k: SortKey) => (sortKey.value === k ? (sortDesc.value ? 'descending' : 'ascending') : 'none')

const card = 'rounded-md border border-pico-border bg-pico-panel p-3'
const table = 'w-full rounded-md border border-pico-border bg-pico-panel text-left text-sm text-zinc-300'
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main class="mx-auto max-w-5xl space-y-6 px-4 py-6 sm:px-6">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <h1 class="text-lg font-semibold text-white">Cluster health</h1>
        <div class="flex items-center gap-3 text-xs text-zinc-500" aria-live="polite">
          <span v-if="updated">Updated {{ updated.toLocaleTimeString() }} (auto 15s)</span>
          <button
            class="rounded border border-pico-border px-2 py-1 text-zinc-300 hover:bg-pico-card disabled:opacity-50"
            :disabled="loading"
            @click="load"
          >
            {{ loading ? 'Refreshing…' : 'Refresh' }}
          </button>
        </div>
      </div>

      <div v-if="error" role="alert" class="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
        {{ error }}
      </div>

      <template v-if="data">
        <div v-if="data.errors.length" role="alert" class="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-400">
          <div class="font-semibold">Warnings</div>
          <ul class="list-inside list-disc">
            <li v-for="(e, i) in data.errors" :key="i">{{ e }}</li>
          </ul>
        </div>

        <section class="grid grid-cols-2 gap-3 sm:grid-cols-4" aria-label="Summary">
          <div :class="card">
            <div class="text-xs text-zinc-500">Nodes</div>
            <div class="text-xl font-semibold text-white">
              {{ data.cluster.node_count }}<span v-if="data.cluster.max_nodes !== null" class="text-sm text-zinc-500"> / {{ data.cluster.max_nodes }}</span>
            </div>
            <div class="text-xs text-zinc-500">
              <template v-if="data.cluster.max_nodes !== null">
                {{ Math.max(0, data.cluster.max_nodes - data.cluster.node_count) }} autoscaler headroom
              </template>
              <template v-else>autoscaler max not reported</template>
            </div>
          </div>
          <div :class="card">
            <div class="text-xs text-zinc-500">Pods</div>
            <div class="text-xl font-semibold text-white">{{ data.cluster.pods_total }}</div>
          </div>
          <div :class="card">
            <div class="text-xs text-zinc-500">Metrics</div>
            <div :class="['text-xl font-semibold', data.metrics_available ? 'text-status-running' : 'text-amber-400']">
              {{ data.metrics_available ? 'Available' : 'Unavailable' }}
            </div>
            <div v-if="!data.metrics_available" class="text-xs text-zinc-500">usage shown as n/a</div>
          </div>
          <div :class="card">
            <div class="text-xs text-zinc-500">Generated</div>
            <div class="text-sm text-white">{{ new Date(data.generated_at).toLocaleTimeString() }}</div>
          </div>
        </section>

        <section>
          <h2 class="mb-2 text-sm font-semibold text-zinc-300">Nodes</h2>
          <div class="grid gap-3 sm:grid-cols-2">
            <div v-for="n in data.nodes" :key="n.name" :class="card">
              <div class="mb-2 flex items-center justify-between gap-2">
                <span class="truncate font-mono text-xs text-white" :title="n.name">{{ n.name }}</span>
                <span
                  :class="[
                    'rounded-full border px-2 py-0.5 text-xs',
                    n.ready ? 'border-status-running/30 bg-status-running/15 text-status-running' : 'border-status-error/30 bg-status-error/15 text-status-error',
                  ]"
                >
                  {{ n.ready ? 'Ready' : 'Not ready' }}
                </span>
              </div>
              <template v-for="r in [
                { label: 'CPU', unit: 'm', cap: n.cpu_allocatable_m, req: n.cpu_requested_m, used: n.cpu_used_m },
                { label: 'Memory', unit: 'Mi', cap: n.mem_allocatable_mi, req: n.mem_requested_mi, used: n.mem_used_mi },
              ]" :key="r.label">
                <div class="mb-1 flex justify-between text-xs text-zinc-400">
                  <span>{{ r.label }}</span>
                  <span>used {{ num(r.used, r.unit) }} · req {{ r.req }}{{ r.unit }} · alloc {{ r.cap }}{{ r.unit }}</span>
                </div>
                <div
                  class="relative mb-2 h-3 overflow-hidden rounded bg-pico-card"
                  role="img"
                  :aria-label="`${r.label}: used ${num(r.used, r.unit)}, requested ${r.req}${r.unit}, allocatable ${r.cap}${r.unit}`"
                >
                  <div class="absolute inset-y-0 left-0 bg-zinc-600" :style="{ width: w(r.req, r.cap) }" title="requested" />
                  <div
                    v-if="r.used !== null"
                    :class="['absolute inset-y-0 left-0 opacity-90', hot(pct(r.used, r.cap)) ? 'bg-amber-500' : 'bg-accent']"
                    :style="{ width: w(r.used, r.cap) }"
                    title="used"
                  />
                </div>
              </template>
              <div
                v-if="pressure(n).tight"
                class="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-xs text-amber-400"
              >
                Requested {{ pressure(n).c }}% CPU, {{ pressure(n).m }}% memory: next app may trigger a new node
              </div>
              <div class="flex justify-between text-xs text-zinc-500">
                <span>Pods {{ n.pods }}/{{ n.pods_capacity }}</span>
                <span>Age {{ age(n.age_s) }}</span>
              </div>
            </div>
            <div v-if="data.nodes.length === 0" class="text-sm text-zinc-500">No nodes reported.</div>
          </div>
          <p class="mt-1 text-xs text-zinc-500">Bars: grey = requested (drives scheduling), colored = used, full width = allocatable.</p>
        </section>

        <section>
          <h2 class="mb-2 text-sm font-semibold text-zinc-300">Apps</h2>
          <div class="overflow-x-auto">
            <table :class="table">
              <thead class="text-xs text-zinc-500">
                <tr>
                  <th class="p-2">App</th><th class="p-2">Owner</th><th class="p-2">Status</th><th class="p-2">Pods</th><th class="p-2">Restarts</th>
                  <th class="p-2" :aria-sort="ariaSort('cpu')">
                    <button class="hover:text-white" @click="sortBy('cpu')">CPU {{ sortKey === 'cpu' ? (sortDesc ? '▼' : '▲') : '' }}</button>
                  </th>
                  <th class="p-2" :aria-sort="ariaSort('mem')">
                    <button class="hover:text-white" @click="sortBy('mem')">Memory {{ sortKey === 'mem' ? (sortDesc ? '▼' : '▲') : '' }}</button>
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="a in sortedApps" :key="a.id" class="border-t border-pico-border">
                  <td class="p-2">{{ a.name }}<div class="font-mono text-xs text-zinc-500">{{ a.namespace }}</div></td>
                  <td class="p-2">{{ a.owner_email }}</td>
                  <td class="p-2"><StatusBadge :status="a.status" /></td>
                  <td class="p-2" :class="a.pods_ready < a.pods_total ? 'text-amber-400' : ''">{{ a.pods_ready }}/{{ a.pods_total }}</td>
                  <td class="p-2" :class="a.restarts > 0 ? 'font-semibold text-amber-400' : ''">{{ a.restarts }}</td>
                  <td class="whitespace-nowrap p-2" :class="hot(pct(a.cpu_used_m, a.cpu_limit_m)) ? 'font-semibold text-amber-400' : ''">
                    <template v-if="a.cpu_used_m !== null">{{ fmtUse(a.cpu_used_m, a.cpu_limit_m, 'm') }}</template>
                    <template v-else><span :title="NA">—</span> <span class="text-xs font-normal text-zinc-500">limit {{ a.cpu_limit_m }}m</span></template>
                  </td>
                  <td class="whitespace-nowrap p-2" :class="hot(pct(a.mem_used_mi, a.mem_limit_mi)) ? 'font-semibold text-amber-400' : ''">
                    <template v-if="a.mem_used_mi !== null">{{ fmtUse(a.mem_used_mi, a.mem_limit_mi, 'Mi') }}</template>
                    <template v-else><span :title="NA">—</span> <span class="text-xs font-normal text-zinc-500">limit {{ a.mem_limit_mi }}Mi</span></template>
                  </td>
                </tr>
                <tr v-if="sortedApps.length === 0"><td colspan="7" class="p-3 text-zinc-500">No apps.</td></tr>
              </tbody>
            </table>
          </div>
        </section>

        <section>
          <h2 class="mb-2 text-sm font-semibold text-zinc-300">Platform components</h2>
          <div class="overflow-x-auto">
            <table :class="table">
              <thead class="text-xs text-zinc-500">
                <tr><th class="p-2">Name</th><th class="p-2">Namespace</th><th class="p-2">Ready</th><th class="p-2">Restarts</th><th class="p-2">CPU</th><th class="p-2">Memory</th></tr>
              </thead>
              <tbody>
                <tr v-for="p in data.platform" :key="p.namespace + p.name" class="border-t border-pico-border">
                  <td class="p-2">{{ p.name }}</td>
                  <td class="p-2 font-mono text-xs">{{ p.namespace }}</td>
                  <td class="p-2" :class="p.ready ? 'text-status-running' : 'font-semibold text-status-error'">{{ p.ready ? 'Ready' : 'Not ready' }}</td>
                  <td class="p-2" :class="p.restarts > 0 ? 'font-semibold text-amber-400' : ''">{{ p.restarts }}</td>
                  <td class="p-2"><span v-if="p.cpu_used_m === null" :title="NA">—</span><template v-else>{{ p.cpu_used_m }}m</template></td>
                  <td class="p-2"><span v-if="p.mem_used_mi === null" :title="NA">—</span><template v-else>{{ p.mem_used_mi }}Mi</template></td>
                </tr>
                <tr v-if="data.platform.length === 0"><td colspan="6" class="p-3 text-zinc-500">None reported.</td></tr>
              </tbody>
            </table>
          </div>
        </section>

        <section>
          <h2 class="mb-2 text-sm font-semibold text-zinc-300">DB servers</h2>
          <div class="overflow-x-auto">
            <table :class="table">
              <thead class="text-xs text-zinc-500">
                <tr><th class="p-2">Name</th><th class="p-2">Host</th><th class="p-2">Capacity</th><th class="p-2">Status</th></tr>
              </thead>
              <tbody>
                <tr v-for="s in data.db_servers" :key="s.id" class="border-t border-pico-border">
                  <td class="p-2">{{ s.name }}</td>
                  <td class="p-2 font-mono text-xs">{{ s.host }}:{{ s.port }}</td>
                  <td class="p-2"><DbCapacityBar :server="s" /></td>
                  <td class="p-2">{{ s.status }}</td>
                </tr>
                <tr v-if="data.db_servers.length === 0"><td colspan="4" class="p-3 text-zinc-500">No DB servers.</td></tr>
              </tbody>
            </table>
          </div>
        </section>
      </template>
      <p v-else-if="!error" class="text-sm text-zinc-500">Loading…</p>
    </main>
  </div>
</template>
