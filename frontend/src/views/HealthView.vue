<script setup lang="ts">
import type { DbServer } from '../types'
const connHigh = (s: DbServer) => !!s.connections && s.connections.max > 0 && s.connections.used / s.connections.max > 0.8
const mb = (b: number) => (b / 1048576).toFixed(1)
const HOST_KEYS = [
  { key: 'cpu_pct', label: 'cpu' },
  { key: 'mem_pct', label: 'mem' },
  { key: 'disk_pct', label: 'disk' },
] as const
const hostPct = (v: number | null | undefined) => (v == null ? 'n/a' : `${Math.round(v)}%`)
const hostHigh = (v: number | null | undefined) => v != null && v > 80
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Check, RefreshCw } from 'lucide-vue-next'
import IconButton from '../components/IconButton.vue'
import NavBar from '../components/NavBar.vue'
import StatusBadge from '../components/StatusBadge.vue'
import DbCapacityBar from '../components/DbCapacityBar.vue'
import { approveUser, getHealth } from '../api/admin'
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
const approving = ref<number | null>(null)
async function approve(id: number) {
  approving.value = id
  try {
    await approveUser(id)
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Approve failed'
  } finally {
    approving.value = null
  }
}
const waiting = (iso: string) => age(Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000))
const statuses = ['running', 'deploying', 'pending', 'error', 'suspended'] as const
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
const cpuCap = (a: HealthApp): number => a.cpu_capacity_m ?? a.cpu_limit_m
const memCap = (a: HealthApp): number => a.mem_capacity_mi ?? a.mem_limit_mi
const usage = (a: HealthApp, k: SortKey) => (k === 'cpu' ? pct(a.cpu_used_m, cpuCap(a)) : pct(a.mem_used_mi, memCap(a)))
const podTitle = (a: HealthApp) =>
  (a.pod_usage ?? []).map((p) => `${p.name}: ${p.ready ? 'ready' : 'not ready'}, ${p.restarts} restarts, ${p.cpu_used_m ?? 'n/a'}m, ${p.mem_used_mi ?? 'n/a'}Mi`).join('\n') || undefined
const podsWanted = (a: HealthApp) => (a.replicas && a.replicas !== a.pods_total ? ` (want ${a.replicas})` : '')
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

// Backend error strings are developer-facing ("cluster snapshot failed (ApiException)"); say what it means.
function humanize(e: string): string {
  if (/metrics\.k8s\.io/i.test(e)) return 'Metrics are unavailable. CPU and memory usage show as n/a until the metrics server responds.'
  if (/cluster snapshot/i.test(e)) return "Can't reach the Kubernetes API. Metrics and node data are unavailable."
  return e
}
const humanErrors = computed(() => [...new Set((data.value?.errors ?? []).map(humanize))])
const otherErrors = computed(() => humanErrors.value.filter((e) => !/Kubernetes API/.test(e))) // that one is in the verdict row

// Verdict: every problem links to where it is.
interface Problem { text: string; target: string }
const problems = computed<Problem[]>(() => {
  const d = data.value
  if (!d) return []
  const out: Problem[] = []
  if (humanErrors.value.some((e) => /Kubernetes API/.test(e))) out.push({ text: "Can't reach the Kubernetes API (metrics and node data unavailable)", target: 'sec-nodes' })
  const down = d.nodes.filter((n) => !n.ready)
  if (down.length) out.push({ text: `${down.length} node${down.length > 1 ? 's' : ''} not ready`, target: 'sec-nodes' })
  const diskP = d.nodes.filter((n) => n.conditions?.DiskPressure)
  if (diskP.length) out.push({ text: 'node disk nearly full (DiskPressure)', target: 'sec-nodes' })
  const tight = d.nodes.filter((n) => pressure(n).tight)
  if (tight.length) out.push({ text: `${tight.length} node${tight.length > 1 ? 's' : ''} nearly full`, target: 'sec-nodes' })
  const failed = d.apps.filter((a) => a.status === 'error')
  if (failed.length) out.push({ text: `${failed.length} failed app${failed.length > 1 ? 's' : ''}`, target: 'sec-apps' })
  const notReady = d.platform.filter((p) => !p.ready)
  // Names, so a pod that is just starting during a platform deploy is recognisable as such.
  if (notReady.length) out.push({ text: `Platform pod${notReady.length > 1 ? 's' : ''} not ready: ${notReady.map((p) => p.name).join(', ')}`, target: 'sec-platform' })
  const full = d.db_servers.filter((s) => s.warn)
  if (full.length) out.push({ text: `${full.length} database server${full.length > 1 ? 's' : ''} above 80%`, target: 'sec-db' })
  if (!d.metrics_available && !out.some((p) => /Kubernetes/.test(p.text))) out.push({ text: 'Metrics unavailable', target: 'sec-summary' })
  return out
})
const pendingN = computed(() => data.value?.users?.pending ?? data.value?.pending_users?.length ?? 0)
function jump(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

const card = 'min-w-0 rounded-md border border-pico-border bg-pico-panel p-3'
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main id="main" tabindex="-1" class="mx-auto max-w-6xl space-y-4 px-4 py-4 sm:px-6">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <h1 class="page-title">Cluster health</h1>
        <div class="flex items-center gap-3 text-xs text-zinc-400" aria-live="polite">
          <span v-if="updated">Updated {{ updated.toLocaleTimeString() }} (auto 15s)</span>
          <IconButton :icon="RefreshCw" :label="loading ? 'Refreshing…' : 'Refresh'" :disabled="loading" @click="load" />
        </div>
      </div>

      <div v-if="error" role="alert" class="alert-error">{{ error }}</div>

      <template v-if="data">
        <div
          :class="problems.length || pendingN ? 'alert-warn' : 'alert-success'"
          class="flex flex-wrap items-center gap-x-3 gap-y-1"
          role="status"
          aria-label="Overall status"
        >
          <template v-if="!problems.length">
            <strong>All systems normal</strong>
            <span v-if="!pendingN" class="text-xs opacity-80">Nodes ready, no failed apps, database capacity fine.</span>
          </template>
          <template v-else>
            <strong>{{ problems.length }} {{ problems.length === 1 ? 'problem' : 'problems' }}:</strong>
            <button v-for="p in problems" :key="p.text" type="button" class="underline underline-offset-2" @click="jump(p.target)">{{ p.text }}</button>
          </template>
          <RouterLink v-if="pendingN" to="/admin" class="underline underline-offset-2">{{ pendingN }} {{ pendingN === 1 ? 'user' : 'users' }} waiting for approval</RouterLink>
        </div>

        <div v-if="otherErrors.length" role="alert" class="alert-warn">
          <ul class="list-inside list-disc">
            <li v-for="e in otherErrors" :key="e">{{ e }}</li>
          </ul>
        </div>

        <section v-if="data.users || data.workloads" class="grid grid-cols-2 gap-3 sm:grid-cols-4" aria-label="Users and workloads">
          <div v-if="data.users" :class="card">
            <div class="text-xs text-zinc-400">Users</div>
            <div class="text-lg font-semibold text-white">{{ data.users.total }}</div>
            <div class="text-xs text-zinc-400">{{ data.users.active }} active · {{ data.users.suspended }} suspended</div>
          </div>
          <div v-if="data.users" :class="[card, data.users.pending > 0 ? 'border-amber-500/40' : '']">
            <div class="text-xs text-zinc-400">Pending approval</div>
            <div :class="['text-lg font-semibold', data.users.pending > 0 ? 'text-amber-400' : 'text-white']">{{ data.users.pending }}</div>
          </div>
          <div v-if="data.workloads" :class="[card, 'col-span-2']">
            <div class="text-xs text-zinc-400">Workloads</div>
            <div class="text-lg font-semibold text-white">{{ data.workloads.apps_total }} <span class="text-sm font-normal text-zinc-400">apps</span></div>
            <div class="flex flex-wrap gap-x-3 text-xs text-zinc-400">
              <span
                v-for="st in statuses"
                :key="st"
                :class="st === 'error' && (data.workloads.by_status[st] ?? 0) > 0 ? 'font-semibold text-status-error' : ''"
              >{{ data.workloads.by_status[st] ?? 0 }} {{ st }}</span>
            </div>
            <div class="text-xs text-zinc-400">{{ data.workloads.with_db }} with database · {{ data.workloads.with_storage }} with storage</div>
          </div>
        </section>

        <section v-if="data.pending_users?.length" aria-label="Waiting for approval">
          <div class="mb-2 flex items-center justify-between">
            <h2 class="text-sm font-semibold text-zinc-300">Waiting for approval</h2>
            <RouterLink to="/admin" class="link text-xs">Manage users</RouterLink>
          </div>
          <ul class="divide-y divide-pico-border rounded-md border border-pico-border bg-pico-panel text-sm text-zinc-300">
            <li v-for="u in data.pending_users" :key="u.id" class="flex items-center justify-between gap-2 px-3 py-1">
              <span class="min-w-0 truncate">{{ u.email }}</span>
              <span class="flex shrink-0 items-center gap-3">
                <span class="mono text-zinc-400">waiting {{ waiting(u.created_at) }}</span>
                <IconButton :icon="Check" :label="approving === u.id ? 'Approving…' : `Approve ${u.email}`" sm :disabled="approving === u.id" @click="approve(u.id)" />
              </span>
            </li>
          </ul>
        </section>
        <div v-else-if="data.users" class="text-right text-xs"><RouterLink to="/admin" class="link">Admin page</RouterLink></div>

        <section id="sec-summary" class="grid grid-cols-2 gap-3 sm:grid-cols-3" aria-label="Summary">
          <div :class="card">
            <div class="text-xs text-zinc-400">Nodes</div>
            <div class="text-lg font-semibold text-white">
              {{ data.cluster.node_count }}<span v-if="data.cluster.max_nodes !== null" class="text-sm text-zinc-400"> / {{ data.cluster.max_nodes }}</span>
            </div>
            <div class="text-xs text-zinc-400">
              <template v-if="data.cluster.max_nodes !== null">
                {{ Math.max(0, data.cluster.max_nodes - data.cluster.node_count) }} autoscaler headroom
              </template>
              <template v-else>autoscaler max not reported</template>
            </div>
          </div>
          <div :class="card">
            <div class="text-xs text-zinc-400">Pods</div>
            <div class="text-lg font-semibold text-white">{{ data.cluster.pods_total }}</div>
          </div>
          <div :class="card">
            <div class="text-xs text-zinc-400">Metrics</div>
            <div :class="['text-lg font-semibold', data.metrics_available ? 'text-status-running' : 'text-amber-400']">
              {{ data.metrics_available ? 'Available' : 'Unavailable' }}
            </div>
            <div v-if="!data.metrics_available" class="text-xs text-zinc-400">usage shown as n/a</div>
          </div>
        </section>

        <section id="sec-nodes" class="scroll-mt-4">
          <h2 class="mb-1.5 text-sm font-semibold text-zinc-300">Nodes</h2>
          <div class="grid gap-3 sm:grid-cols-2">
            <div v-for="n in data.nodes" :key="n.name" :class="card">
              <div class="mb-2 flex items-center justify-between gap-2">
                <span class="mono truncate text-white" :title="n.name">{{ n.name }}</span>
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
                <div class="mb-1 flex flex-wrap justify-between gap-x-3 text-xs text-zinc-400">
                  <span>{{ r.label }}</span>
                  <span class="mono">used {{ num(r.used, r.unit) }} · req {{ r.req }}{{ r.unit }} · alloc {{ r.cap }}{{ r.unit }}</span>
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
              <div class="mb-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-400">
                <span v-for="c in ['DiskPressure', 'MemoryPressure', 'PIDPressure']" :key="c" class="mono" :class="n.conditions?.[c] ? 'font-semibold text-status-error' : ''">
                  {{ c }} {{ n.conditions ? (n.conditions[c] ? 'YES' : 'no') : 'n/a' }}
                </span>
                <span v-if="n.disk_allocatable_mi != null" class="mono">Disk alloc {{ n.disk_allocatable_mi }}Mi / cap {{ n.disk_capacity_mi }}Mi</span>
              </div>
              <div class="flex justify-between text-xs text-zinc-400">
                <span>Pods {{ n.pods }}/{{ n.pods_capacity }}</span>
                <span>Age {{ age(n.age_s) }}</span>
              </div>
            </div>
            <div v-if="data.nodes.length === 0" class="text-sm text-zinc-400">No nodes reported.</div>
          </div>
          <p class="mt-1 text-xs text-zinc-400">Bars: grey = requested (drives scheduling), colored = used, full width = allocatable.</p>
        </section>

        <section id="sec-apps" class="scroll-mt-4">
          <h2 class="mb-1.5 text-sm font-semibold text-zinc-300">Apps</h2>
          <div class="card overflow-x-auto">
            <table class="tbl min-w-[60rem]">
              <colgroup><col /><col class="w-44" /><col class="w-28" /><col class="w-16" /><col class="w-20" /><col class="w-40" /><col class="w-48" /></colgroup>
              <thead>
                <tr>
                  <th scope="col">App</th><th scope="col">Owner</th><th scope="col">Status</th>
                  <th scope="col" class="text-right">Pods</th><th scope="col" class="text-right">Restarts</th>
                  <th scope="col" class="text-right" :aria-sort="ariaSort('cpu')">
                    <button class="uppercase tracking-wide hover:text-white" @click="sortBy('cpu')">CPU {{ sortKey === 'cpu' ? (sortDesc ? '▼' : '▲') : '' }}</button>
                  </th>
                  <th scope="col" class="text-right" :aria-sort="ariaSort('mem')">
                    <button class="uppercase tracking-wide hover:text-white" @click="sortBy('mem')">Memory {{ sortKey === 'mem' ? (sortDesc ? '▼' : '▲') : '' }}</button>
                  </th>
                </tr>
              </thead>
              <tbody class="text-zinc-300">
                <tr v-for="a in sortedApps" :key="a.id">
                  <td class="truncate" :title="`${a.name} (${a.namespace})`"><span class="text-white">{{ a.name }}</span><div class="mono truncate text-zinc-400">{{ a.namespace }}</div></td>
                  <td class="truncate" :title="a.owner_email">{{ a.owner_email }}</td>
                  <td><StatusBadge :status="a.status" /></td>
                  <td class="mono text-right" :class="a.pods_ready < a.pods_total ? 'text-amber-400' : ''" :title="podTitle(a)">{{ a.pods_ready }}/{{ a.pods_total }}{{ podsWanted(a) }}</td>
                  <td class="mono text-right" :class="a.restarts > 0 ? 'font-semibold text-amber-400' : ''">{{ a.restarts }}</td>
                  <td class="mono whitespace-nowrap text-right" :class="hot(pct(a.cpu_used_m, cpuCap(a))) ? 'font-semibold text-amber-400' : ''">
                    <template v-if="a.cpu_used_m !== null">{{ fmtUse(a.cpu_used_m, cpuCap(a), 'm') }}</template>
                    <template v-else><span :title="NA">—</span> <span class="font-normal text-zinc-400">/ {{ cpuCap(a) }}m</span></template>
                  </td>
                  <td class="mono whitespace-nowrap text-right" :class="hot(pct(a.mem_used_mi, memCap(a))) ? 'font-semibold text-amber-400' : ''">
                    <template v-if="a.mem_used_mi !== null">{{ fmtUse(a.mem_used_mi, memCap(a), 'Mi') }}</template>
                    <template v-else><span :title="NA">—</span> <span class="font-normal text-zinc-400">/ {{ memCap(a) }}Mi</span></template>
                  </td>
                </tr>
                <tr v-if="sortedApps.length === 0"><td colspan="7" class="text-zinc-400">No apps.</td></tr>
              </tbody>
            </table>
          </div>
        </section>

        <section id="sec-platform" class="scroll-mt-4">
          <h2 class="mb-1.5 text-sm font-semibold text-zinc-300">Platform components</h2>
          <div class="card overflow-x-auto">
            <table class="tbl min-w-[56rem]">
              <colgroup><col /><col class="w-40" /><col class="w-24" /><col class="w-20" /><col class="w-24" /><col class="w-24" /></colgroup>
              <thead>
                <tr>
                  <th scope="col">Name</th><th scope="col">Namespace</th><th scope="col">Ready</th>
                  <th scope="col" class="text-right">Restarts</th><th scope="col" class="text-right">CPU</th><th scope="col" class="text-right">Memory</th>
                </tr>
              </thead>
              <tbody class="text-zinc-300">
                <tr v-for="p in data.platform" :key="p.namespace + p.name">
                  <td class="truncate" :title="p.name">{{ p.name }}</td>
                  <td class="mono truncate" :title="p.namespace">{{ p.namespace }}</td>
                  <td :class="p.ready ? 'text-status-running' : 'font-semibold text-status-error'">{{ p.ready ? 'Ready' : 'Not ready' }}</td>
                  <td class="mono text-right" :class="p.restarts > 0 ? 'font-semibold text-amber-400' : ''">{{ p.restarts }}</td>
                  <td class="mono text-right"><span v-if="p.cpu_used_m === null" :title="NA">—</span><template v-else>{{ p.cpu_used_m }}m</template></td>
                  <td class="mono text-right"><span v-if="p.mem_used_mi === null" :title="NA">—</span><template v-else>{{ p.mem_used_mi }}Mi</template></td>
                </tr>
                <tr v-if="data.platform.length === 0"><td colspan="6" class="text-zinc-400">None reported.</td></tr>
              </tbody>
            </table>
          </div>
        </section>

        <section id="sec-db" class="scroll-mt-4">
          <h2 class="mb-1.5 text-sm font-semibold text-zinc-300">Database servers</h2>
          <div class="card overflow-x-auto">
            <table class="tbl min-w-[68rem]">
              <colgroup><col class="w-32" /><col /><col class="w-44" /><col class="w-52" /><col class="w-64" /><col class="w-24" /></colgroup>
              <thead>
                <tr><th scope="col">Name</th><th scope="col">Host</th><th scope="col">Capacity</th><th scope="col">Connections</th><th scope="col">Data</th><th scope="col">Status</th></tr>
              </thead>
              <tbody class="text-zinc-300">
                <tr v-for="s in data.db_servers" :key="s.id">
                  <td class="truncate" :title="s.name">{{ s.name }}</td>
                  <td class="mono truncate" :title="`${s.host}:${s.port}`">{{ s.host }}:{{ s.port }}</td>
                  <td><DbCapacityBar :server="s" /></td>
                  <td class="mono whitespace-nowrap" :title="s.error ?? ''">
                    <template v-if="s.connections">
                      <span :class="connHigh(s) ? 'text-amber-400' : ''">{{ s.connections.used }} / {{ s.connections.max }}</span>
                      <span class="text-zinc-500"> (peak {{ s.connections.max_used }})</span>
                    </template>
                    <span v-else class="text-zinc-500">n/a</span>
                  </td>
                  <td class="mono whitespace-nowrap" :title="s.error ?? ''">
                    <template v-if="s.data_size_bytes != null">
                      {{ mb(s.data_size_bytes) }} MB<template v-if="s.disk_mb"> / {{ s.disk_mb }} MB ({{ Math.round((s.data_size_bytes / 1048576 / s.disk_mb) * 100) }}%)</template>
                    </template>
                    <span v-else class="text-zinc-500">n/a</span>
                    <!-- Host cpu/mem/disk only render when present. DO managed MySQL doesn't serve the scrapable
                         metrics endpoint, so this stays hidden rather than showing a permanent n/a. -->
                    <div v-if="s.host_metrics" class="text-xs">
                      <template v-for="(k, i) in HOST_KEYS" :key="k.key">
                        <span v-if="i" class="text-zinc-600"> · </span>
                        <span class="text-zinc-500">{{ k.label }} </span><span :class="hostHigh(s.host_metrics[k.key]) ? 'text-amber-400' : ''">{{ hostPct(s.host_metrics[k.key]) }}</span>
                      </template>
                    </div>
                  </td>
                  <td>{{ s.status }}</td>
                </tr>
                <tr v-if="data.db_servers.length === 0"><td colspan="6" class="text-zinc-400">No database servers.</td></tr>
              </tbody>
            </table>
          </div>
        </section>
      </template>
      <p v-else-if="!error" class="text-sm text-zinc-400" role="status">Querying cluster…</p>
    </main>
  </div>
</template>
