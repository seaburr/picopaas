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
import IconButton from '../components/IconButton.vue'
import StatusBadge from '../components/StatusBadge.vue'
import DbCapacityBar from '../components/DbCapacityBar.vue'
import HealthBackups from '../components/HealthBackups.vue'
import HealthLogs from '../components/HealthLogs.vue'
import { approveUser, getHealth } from '../api/admin'
import { ApiError } from '../api/client'
import type { ClusterHealth, HealthApp } from '../types'
import { parseTs } from '../composables/appUi'
import { fmtTime } from '../utils/tz'

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
const waiting = (iso: string) => age(Math.max(0, (Date.now() - parseTs(iso).getTime()) / 1000))
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
const otherErrors = computed(() => humanErrors.value.filter((e) => !/Kubernetes API|Metrics are unavailable/.test(e))) // those two are built into the problem list

// Verdict: every problem links to where it is.
interface Problem { text: string; detail?: string; target?: string }
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
  if (!d.metrics_available && !out.some((p) => /Kubernetes/.test(p.text))) out.push({ text: 'Metrics unavailable', detail: 'CPU and memory usage show as n/a until the metrics server responds.', target: 'sec-summary' })
  for (const e of otherErrors.value) out.push({ text: e })
  return out
})
const problemCount = computed(() => problems.value.length + (pendingN.value ? 1 : 0))
const pendingN = computed(() => data.value?.users?.pending ?? data.value?.pending_users?.length ?? 0)
function jump(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
</script>

<template>
  <main id="main" tabindex="-1" class="page-main space-y-4">
    <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
      <h1 class="page-title">Cluster health</h1>
      <span v-if="data" class="st text-sm" :class="problemCount ? 'tone-warn' : 'tone-ok'">
        <span class="dot" :class="problemCount ? 'dot-warn' : 'dot-ok'" aria-hidden="true" />{{ problemCount ? `${problemCount} ${problemCount === 1 ? 'problem' : 'problems'}` : 'All systems normal' }}
      </span>
      <span class="meta" aria-live="polite"><template v-if="updated">Updated {{ fmtTime(updated) }} (auto 15s)</template></span>
      <span class="flex-1" />
      <IconButton label="Refresh" text="Refresh" :busy="loading" @click="load" />
    </div>

    <div v-if="error" role="alert" class="alert-error">{{ error }}</div>

    <template v-if="data">
      <div v-if="problems.length || pendingN" class="alert-warn" role="status" aria-label="Overall status">
        <strong>{{ problemCount }} {{ problemCount === 1 ? 'problem' : 'problems' }}</strong>
        <ul class="mt-1 space-y-0.5">
          <li v-for="p in problems" :key="p.text">
            <button v-if="p.target" type="button" class="text-left underline underline-offset-2 max-md:min-h-[36px]" @click="jump(p.target)">{{ p.text }}</button>
            <template v-else>{{ p.text }}</template>
            <span v-if="p.detail">: {{ p.detail }}</span>
          </li>
          <li v-if="pendingN"><RouterLink to="/admin" class="underline underline-offset-2">{{ pendingN }} {{ pendingN === 1 ? 'user' : 'users' }} waiting for approval</RouterLink></li>
        </ul>
      </div>
      <p v-else class="sr-only" role="status" aria-label="Overall status">All systems normal. Nodes ready, no failed apps, database capacity fine.</p>

      <section id="sec-summary" class="sec pt-3" aria-label="Summary">
        <dl class="grid grid-cols-2 gap-x-6 gap-y-3 min-[760px]:grid-cols-4 min-[1100px]:grid-cols-7">
          <div>
            <dt class="meta">Nodes</dt>
            <dd class="mono text-base">{{ data.cluster.node_count }}<template v-if="data.cluster.max_nodes !== null"> / {{ data.cluster.max_nodes }}</template></dd>
            <dd class="meta">{{ data.cluster.max_nodes !== null ? `${Math.max(0, data.cluster.max_nodes - data.cluster.node_count)} headroom` : 'max not reported' }}</dd>
          </div>
          <div><dt class="meta">Pods</dt><dd class="mono text-base">{{ data.cluster.pods_total }}</dd></div>
          <div>
            <dt class="meta">Metrics</dt>
            <dd class="text-base" :class="data.metrics_available ? '' : 'tone-warn'">{{ data.metrics_available ? 'Available' : 'Unavailable' }}</dd>
            <dd v-if="!data.metrics_available" class="meta">usage shown as n/a</dd>
          </div>
          <template v-if="data.workloads">
            <div>
              <dt class="meta">Apps</dt>
              <dd class="mono text-base">{{ data.workloads.apps_total }}</dd>
              <dd class="meta">{{ data.workloads.by_status.running ?? 0 }} running</dd>
            </div>
            <div>
              <dt class="meta">Failing</dt>
              <dd class="mono text-base" :class="(data.workloads.by_status.error ?? 0) > 0 ? 'text-status-error font-semibold' : ''">{{ data.workloads.by_status.error ?? 0 }}</dd>
              <dd class="meta">{{ (data.workloads.by_status.pending ?? 0) + (data.workloads.by_status.deploying ?? 0) }} starting</dd>
              <dd class="meta">{{ data.workloads.by_status.suspended ?? 0 }} suspended</dd>
            </div>
          </template>
          <template v-if="data.users">
            <div>
              <dt class="meta">Users</dt>
              <dd class="mono text-base">{{ data.users.total }}</dd>
              <dd class="meta">{{ data.users.active }} active</dd>
              <dd class="meta">{{ data.users.suspended }} suspended</dd>
              <dd class="meta">{{ data.users.admins ?? 0 }} {{ (data.users.admins ?? 0) === 1 ? 'admin' : 'admins' }}</dd>
            </div>
            <div>
              <dt class="meta">Pending approval</dt>
              <dd class="mono text-base" :class="data.users.pending > 0 ? 'text-amber-400' : ''">{{ data.users.pending }}</dd>
            </div>
          </template>
        </dl>
        <p v-if="data.workloads" class="meta mt-2">{{ data.workloads.with_db }} with database · {{ data.workloads.with_storage }} with blob storage<template v-if="data.workloads.with_volume"> · {{ data.workloads.with_volume }} with volume</template><template v-if="data.workloads.by_db_engine"> · {{ data.workloads.by_db_engine.mysql }} MySQL · {{ data.workloads.by_db_engine.postgres }} PostgreSQL</template></p>
      </section>

      <section v-if="data.pending_users?.length" class="sec pt-3" aria-label="Waiting for approval">
        <div class="mb-1 flex items-baseline justify-between gap-2">
          <h2 class="section-title">Waiting for approval</h2>
          <RouterLink to="/admin" class="link text-xs">Manage users</RouterLink>
        </div>
        <div class="overflow-x-auto">
          <table class="tbl min-w-96">
            <colgroup><col /><col class="w-32" /><col class="w-28" /></colgroup>
            <thead><tr><th scope="col">User</th><th scope="col" class="text-right">Waiting</th><th scope="col"><span class="sr-only">Action</span></th></tr></thead>
            <tbody>
              <tr v-for="u in data.pending_users" :key="u.id">
                <td class="truncate" :title="u.email">{{ u.email }}</td>
                <td class="mono text-right">{{ waiting(u.created_at) }}</td>
                <td><div class="row-acts"><button type="button" class="row-act" :aria-label="approving === u.id ? 'Approving…' : `Approve ${u.email}`" :disabled="approving === u.id" @click="approve(u.id)">Approve</button></div></td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section id="sec-nodes" class="sec scroll-mt-4 pt-3">
        <h2 class="section-title mb-2">Nodes</h2>
        <div class="overflow-x-auto">
          <table class="tbl min-w-[50rem]">
            <colgroup><col /><col class="w-24" /><col class="w-52" /><col class="w-52" /><col class="w-20" /><col class="w-16" /></colgroup>
            <thead>
              <tr>
                <th scope="col">Node</th><th scope="col">Status</th>
                <th scope="col" class="text-right">CPU used / req / alloc</th><th scope="col" class="text-right">Memory used / req / alloc</th>
                <th scope="col" class="text-right">Pods</th><th scope="col" class="text-right">Age</th>
              </tr>
            </thead>
            <tbody class="text-zinc-300">
              <template v-for="n in data.nodes" :key="n.name"><tr>
                <td class="mono truncate" :title="n.name">{{ n.name }}</td>
                <td><span class="st" :class="n.ready ? 'tone-ok' : 'tone-err'"><span class="dot" :class="n.ready ? 'dot-ok' : 'dot-err'" aria-hidden="true" />{{ n.ready ? 'Ready' : 'Not ready' }}</span></td>
                <td class="mono whitespace-nowrap text-right" :class="hot(pct(n.cpu_used_m, n.cpu_allocatable_m)) ? 'text-amber-400' : ''" :aria-label="`CPU: used ${num(n.cpu_used_m, 'm')}, requested ${n.cpu_requested_m}m, allocatable ${n.cpu_allocatable_m}m`">{{ n.cpu_used_m === null ? 'n/a' : n.cpu_used_m + 'm' }} / {{ n.cpu_requested_m }}m / {{ n.cpu_allocatable_m }}m</td>
                <td class="mono whitespace-nowrap text-right" :class="hot(pct(n.mem_used_mi, n.mem_allocatable_mi)) ? 'text-amber-400' : ''" :aria-label="`Memory: used ${num(n.mem_used_mi, 'Mi')}, requested ${n.mem_requested_mi}Mi, allocatable ${n.mem_allocatable_mi}Mi`">{{ n.mem_used_mi === null ? 'n/a' : n.mem_used_mi + 'Mi' }} / {{ n.mem_requested_mi }}Mi / {{ n.mem_allocatable_mi }}Mi</td>
                <td class="mono text-right">{{ n.pods }}/{{ n.pods_capacity }}</td>
                <td class="mono text-right">{{ age(n.age_s) }}</td>
              </tr>
              <tr><td colspan="6" class="mono !pt-0 text-zinc-400">                  <span v-for="c in ['DiskPressure', 'MemoryPressure', 'PIDPressure']" :key="c" class="mr-2 whitespace-nowrap" :class="n.conditions?.[c] ? 'font-semibold text-status-error' : ''">{{ c }} {{ n.conditions ? (n.conditions[c] ? 'YES' : 'no') : 'n/a' }}</span>
                  <span v-if="n.disk_allocatable_mi != null" class="whitespace-nowrap">Disk alloc {{ n.disk_allocatable_mi }}Mi / cap {{ n.disk_capacity_mi }}Mi</span></td></tr></template>
              <tr v-if="data.nodes.length === 0"><td colspan="6" class="text-zinc-400">No nodes reported.</td></tr>
            </tbody>
          </table>
        </div>
        <p v-for="n in data.nodes.filter((x) => pressure(x).tight)" :key="n.name" class="mt-1 text-xs text-amber-400">
          <span class="mono">{{ n.name }}</span>: requested {{ pressure(n).c }}% CPU, {{ pressure(n).m }}% memory: next app may trigger a new node
        </p>
        <p class="help">Requested drives scheduling; used is the live reading; allocatable is the node's ceiling.</p>
      </section>

      <div class="grid gap-x-7 gap-y-4 min-[1100px]:grid-cols-2">
        <section id="sec-platform" class="sec scroll-mt-4 min-w-0 pt-3">
          <h2 class="section-title mb-2">Platform components</h2>
          <div class="overflow-x-auto">
            <table class="tbl min-w-[40rem]">
              <colgroup><col /><col class="w-32" /><col class="w-24" /><col class="w-16" /><col class="w-20" /><col class="w-24" /></colgroup>
              <thead>
                <tr>
                  <th scope="col">Name</th><th scope="col">Namespace</th><th scope="col">Ready</th>
                  <th scope="col" class="text-right">Restarts</th><th scope="col" class="text-right">CPU used</th><th scope="col" class="text-right">Memory used</th>
                </tr>
              </thead>
              <tbody class="text-zinc-300">
                <tr v-for="p in data.platform" :key="p.namespace + p.name">
                  <td class="mono truncate" :title="p.name">{{ p.name }}</td>
                  <td class="mono truncate" :title="p.namespace">{{ p.namespace }}</td>
                  <td><span class="st" :class="p.ready ? 'tone-ok' : 'tone-err font-semibold'"><span class="dot" :class="p.ready ? 'dot-ok' : 'dot-err'" aria-hidden="true" />{{ p.ready ? 'Ready' : 'Not ready' }}</span></td>
                  <td class="mono text-right" :class="p.restarts > 0 ? 'font-semibold text-amber-400' : ''">{{ p.restarts }}</td>
                  <td class="mono text-right"><span v-if="p.cpu_used_m === null" :title="NA">—</span><template v-else>{{ p.cpu_used_m }}m</template></td>
                  <td class="mono text-right"><span v-if="p.mem_used_mi === null" :title="NA">—</span><template v-else>{{ p.mem_used_mi }}Mi</template></td>
                </tr>
                <tr v-if="data.platform.length === 0"><td colspan="6" class="text-zinc-400">None reported.</td></tr>
              </tbody>
            </table>
          </div>
        </section>

        <section id="sec-db" class="sec scroll-mt-4 min-w-0 pt-3">
          <h2 class="section-title mb-2">Database servers</h2>
          <div class="overflow-x-auto">
            <table class="tbl min-w-[38rem]">
              <colgroup><col class="w-24" /><col /><col class="w-36" /><col class="w-44" /><col class="w-20" /></colgroup>
              <thead>
                <tr><th scope="col">Name</th><th scope="col">Host</th><th scope="col">Capacity</th><th scope="col">Connections</th><th scope="col">Status</th></tr>
              </thead>
              <tbody class="text-zinc-300">
                <template v-for="s in data.db_servers" :key="s.id">
                  <tr>
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
                    <td>{{ s.status }}</td>
                  </tr>
                  <tr>
                    <td colspan="5" class="mono text-zinc-400" :title="s.error ?? ''">
                      Data
                      <template v-if="s.data_size_bytes != null">
                        {{ mb(s.data_size_bytes) }} MB<template v-if="s.disk_mb"> / {{ s.disk_mb }} MB ({{ Math.round((s.data_size_bytes / 1048576 / s.disk_mb) * 100) }}%)</template>
                      </template>
                      <span v-else class="text-zinc-500">n/a</span>
                      <!-- Host cpu/mem/disk only render when present (managed MySQL doesn't serve the metrics endpoint). -->
                      <template v-if="s.host_metrics">
                        <template v-for="k in HOST_KEYS" :key="k.key">
                          <span class="text-zinc-600"> · </span>{{ k.label }} <span :class="hostHigh(s.host_metrics[k.key]) ? 'text-amber-400' : ''">{{ hostPct(s.host_metrics[k.key]) }}</span>
                        </template>
                      </template>
                    </td>
                  </tr>
                </template>
                <tr v-if="data.db_servers.length === 0"><td colspan="5" class="text-zinc-400">No database servers.</td></tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <section id="sec-apps" class="sec scroll-mt-4 pt-3">
        <h2 class="section-title mb-2">Apps</h2>
        <div class="overflow-x-auto">
          <table class="tbl min-w-[60rem]">
            <colgroup><col /><col class="w-44" /><col class="w-28" /><col class="w-20" /><col class="w-20" /><col class="w-40" /><col class="w-48" /></colgroup>
            <thead>
              <tr>
                <th scope="col">App</th><th scope="col">Owner</th><th scope="col">Status</th>
                <th scope="col" class="text-right">Pods</th><th scope="col" class="text-right">Restarts</th>
                <th scope="col" class="text-right" :aria-sort="ariaSort('cpu')">
                  <button class="hover:text-white max-md:min-h-[36px]" @click="sortBy('cpu')">CPU used / limit {{ sortKey === 'cpu' ? (sortDesc ? '▼' : '▲') : '' }}</button>
                </th>
                <th scope="col" class="text-right" :aria-sort="ariaSort('mem')">
                  <button class="hover:text-white max-md:min-h-[36px]" @click="sortBy('mem')">Memory used / limit {{ sortKey === 'mem' ? (sortDesc ? '▼' : '▲') : '' }}</button>
                </th>
              </tr>
            </thead>
            <tbody class="text-zinc-300">
              <tr v-for="a in sortedApps" :key="a.id">
                <td class="truncate" :title="`${a.name} (${a.namespace})`"><span class="mono text-white">{{ a.name }}</span> <span class="mono text-zinc-400">{{ a.namespace }}</span></td>
                <td class="truncate" :title="a.owner_email">{{ a.owner_email }}</td>
                <td><StatusBadge :status="a.status" /></td>
                <td class="mono whitespace-nowrap text-right" :class="a.pods_ready < a.pods_total ? 'text-amber-400' : ''" :title="podTitle(a)">{{ a.pods_ready }}/{{ a.pods_total }}{{ podsWanted(a) }}</td>
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

      <HealthBackups />
      <HealthLogs />

      <section v-if="data.statistics" id="sec-statistics" class="sec scroll-mt-4 pt-3" aria-label="Lifetime statistics">
        <h2 class="section-title">Lifetime statistics</h2>
        <dl class="mt-1 flex flex-wrap gap-x-8 gap-y-1 text-sm">
          <div class="flex items-baseline gap-2"><dt class="meta">Apps ever deployed</dt><dd class="mono">{{ data.statistics.apps_ever_deployed }}</dd><span class="meta">(includes Kaisen previews)</span></div>
          <div class="flex items-baseline gap-2"><dt class="meta">Kaisen apps ever built</dt><dd class="mono">{{ data.statistics.kaisen_apps_ever_built }}</dd><span class="meta">(builds with a finished run)</span></div>
          <div class="flex items-baseline gap-2"><dt class="meta">Kaisen runs</dt><dd class="mono">{{ data.statistics.kaisen_runs_ever }}</dd><span class="meta">(every run started, including retries and changes)</span></div>
        </dl>
        <p class="help">Deleted apps and builds still count.</p>
      </section>
    </template>
    <p v-else-if="!error" class="text-sm text-zinc-400" role="status">Querying cluster…</p>
  </main>
</template>
