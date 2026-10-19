<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import NavBar from '../components/NavBar.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DbCapacityBar from '../components/DbCapacityBar.vue'
import StatusBadge from '../components/StatusBadge.vue'
import ResourcesDialog from '../components/ResourcesDialog.vue'
import {
  approveUser, createDbServer, deleteAdminApp, deleteUser, listAllApps, listAudit, listDbServers, listUsers,
  patchAppResources, patchDbServer, setAppLimit, suspendApp, suspendUser, unsuspendApp, unsuspendUser,
} from '../api/admin'
import { ApiError } from '../api/client'
import type { AdminApp, AdminUser, AuditEntry, DbServer, ResourcesPatch } from '../types'

const users = ref<AdminUser[]>([])
const servers = ref<DbServer[]>([])
const allApps = ref<AdminApp[]>([])
const audit = ref<AuditEntry[]>([])
const auditFilter = ref('')
const pendingUsers = computed(() => users.value.filter((u) => u.status === 'pending'))
const filteredAudit = computed(() => {
  const q = auditFilter.value.trim().toLowerCase()
  if (!q) return audit.value
  return audit.value.filter((a) => a.action.toLowerCase().includes(q) || (a.actor_email ?? '').toLowerCase().includes(q))
})

type Confirm = { title: string; message: string; label: string; run: () => Promise<void> }
const confirm = ref<Confirm | null>(null)
async function runConfirm() {
  const c = confirm.value
  confirm.value = null
  if (c) await act(c.run)
}
async function act(fn: () => Promise<unknown>) {
  error.value = ''
  try {
    await fn()
  } catch (e) {
    error.value = msg(e, 'Action failed')
  }
  await load()
}
function askUser(kind: 'suspend' | 'delete', u: AdminUser) {
  confirm.value =
    kind === 'suspend'
      ? {
          title: `Suspend ${u.email}`,
          message: 'Suspending stops all of this user\'s apps and blocks them from using the platform. Data is kept; unsuspend to restore.',
          label: 'Suspend',
          run: () => suspendUser(u.id),
        }
      : {
          title: `Delete ${u.email}`,
          message: 'Deleting removes this user and permanently destroys all their apps, databases and storage. This cannot be undone.',
          label: 'Delete',
          run: () => deleteUser(u.id),
        }
}
function askApp(kind: 'suspend' | 'delete', a: AdminApp) {
  confirm.value =
    kind === 'suspend'
      ? {
          title: `Suspend ${a.name}`,
          message: 'The app is stopped and its owner cannot edit or unsuspend it.',
          label: 'Suspend',
          run: () => suspendApp(a.id),
        }
      : {
          title: `Delete ${a.name}`,
          message: 'Permanently deletes the app, its database and storage. This cannot be undone.',
          label: 'Delete',
          run: () => deleteAdminApp(a.id),
        }
}
const limits = reactive<Record<number, number>>({})
const error = ref('')
const saving = ref(false)

const emptyForm = () => ({
  name: '',
  host: '',
  port: 3306,
  admin_user: '',
  admin_password: '',
  max_databases: 25,
})
const form = reactive(emptyForm())

const anyWarn = computed(() => servers.value.some((s) => s.warn))
const msg = (e: unknown, fallback: string) => (e instanceof ApiError ? e.message : fallback)

async function load() {
  try {
    ;[users.value, servers.value, allApps.value, audit.value] = await Promise.all([
      listUsers(),
      listDbServers(),
      listAllApps(),
      listAudit(100),
    ])
    for (const u of users.value) limits[u.id] = u.app_limit
  } catch (e) {
    error.value = msg(e, 'Failed to load admin data')
  }
}
onMounted(load)

async function saveLimit(u: AdminUser) {
  error.value = ''
  try {
    const updated = await setAppLimit(u.id, limits[u.id])
    users.value = users.value.map((x) => (x.id === u.id ? { ...x, ...updated } : x))
  } catch (e) {
    error.value = msg(e, 'Failed to update app limit')
  }
}

async function patch(s: DbServer, payload: { status?: string; max_databases?: number }) {
  error.value = ''
  try {
    const updated = await patchDbServer(s.id, payload)
    servers.value = servers.value.map((x) => (x.id === s.id ? updated : x))
  } catch (e) {
    error.value = msg(e, 'Failed to update DB server')
    await load()
  }
}

async function addServer() {
  error.value = ''
  saving.value = true
  try {
    await createDbServer({ ...form })
    Object.assign(form, emptyForm()) // clears the password
    servers.value = await listDbServers()
  } catch (e) {
    error.value = msg(e, 'Failed to add DB server')
  } finally {
    saving.value = false
  }
}

const resApp = ref<AdminApp | null>(null)
const resError = ref('')
async function saveResources(body: ResourcesPatch) {
  if (!resApp.value) return
  resError.value = ''
  try {
    await patchAppResources(resApp.value.id, body)
    resApp.value = null
    await load()
  } catch (e) {
    resError.value = msg(e, 'Failed to update resources')
  }
}
const resSummary = (a: AdminApp) =>
  a.resources
    ? `${a.resources.cpu_request_m}-${a.resources.cpu_limit_m}m CPU, ${a.resources.mem_request_mi}-${a.resources.mem_limit_mi}Mi`
    : ''

const btn = 'rounded border border-pico-border px-2.5 py-1 text-xs text-zinc-300 hover:bg-pico-card'
const btnPrimary = 'rounded bg-accent-dark px-2.5 py-1 text-xs font-medium text-white hover:bg-accent'
const btnDanger = 'rounded border border-danger/40 px-2.5 py-1 text-xs text-danger hover:bg-danger/10'
const input =
  'w-full rounded-md border border-pico-border bg-pico-card px-3 py-2 text-sm text-white placeholder-zinc-600 focus:outline-none focus:border-accent'
const inputSm =
  'rounded-md border border-pico-border bg-pico-card px-2 py-1 text-sm text-white focus:outline-none focus:border-accent'
const card = 'rounded-md border border-pico-border bg-pico-panel'
const th = 'px-3 py-2 text-xs font-medium uppercase tracking-wide text-zinc-500'
const td = 'px-3 py-2.5 align-middle'
const label = 'flex flex-col gap-1 text-xs text-zinc-400'
const labelText = 'text-sm font-medium text-zinc-200'
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main class="mx-auto max-w-5xl space-y-10 px-4 py-6 sm:px-6">
      <h1 class="text-xl font-semibold text-white">Admin</h1>

      <div v-if="anyWarn" class="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-400">
        DB capacity above 80%, file a capacity request.
      </div>
      <div v-if="error" class="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">{{ error }}</div>

      <section v-if="pendingUsers.length" class="space-y-3">
        <h2 class="text-base font-semibold text-amber-400">Pending approval ({{ pendingUsers.length }})</h2>
        <ul class="divide-y divide-pico-border rounded-md border border-amber-500/30 bg-pico-panel">
          <li v-for="u in pendingUsers" :key="u.id" class="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5 text-sm text-zinc-300">
            <div class="min-w-0">
              <div class="break-all">{{ u.email }}</div>
              <div class="text-xs text-zinc-500">Requested {{ u.created_at }}</div>
            </div>
            <button :class="btnPrimary" @click="act(() => approveUser(u.id))">Approve</button>
          </li>
        </ul>
      </section>

      <section class="space-y-3">
        <h2 class="text-base font-semibold text-white">Users <span class="text-sm font-normal text-zinc-500">({{ users.length }})</span></h2>
        <div :class="[card, 'overflow-x-auto']">
          <table class="w-full min-w-[640px] text-left text-sm text-zinc-300">
            <thead><tr><th :class="th">Email</th><th :class="th">Role</th><th :class="th">Apps</th><th :class="th">Status</th><th :class="th">App limit</th><th :class="th"><span class="sr-only">Actions</span></th></tr></thead>
            <tbody>
              <tr v-for="u in users" :key="u.id" class="border-t border-pico-border">
                <td :class="[td, 'break-all']">{{ u.email }}</td>
                <td :class="td">{{ u.role }}</td>
                <td :class="td">{{ u.app_count }}</td>
                <td :class="td"><StatusBadge :status="u.status" /></td>
                <td :class="td">
                  <form class="flex items-center gap-2" @submit.prevent="saveLimit(u)">
                    <input v-model.number="limits[u.id]" type="text" inputmode="numeric" pattern="[0-9]*" aria-label="App limit" :class="[inputSm, 'w-16']" />
                    <button v-if="limits[u.id] !== u.app_limit" type="submit" :class="btnPrimary">Save</button>
                  </form>
                </td>
                <td :class="[td, 'whitespace-nowrap text-right']">
                  <div v-if="u.role !== 'uberadmin'" class="flex justify-end gap-1.5">
                    <button v-if="u.status === 'pending'" :class="btnPrimary" @click="act(() => approveUser(u.id))">Approve</button>
                    <button v-if="u.status === 'suspended'" :class="btn" @click="act(() => unsuspendUser(u.id))">Unsuspend</button>
                    <button v-if="u.status === 'active'" :class="btn" @click="askUser('suspend', u)">Suspend</button>
                    <button :class="btnDanger" @click="askUser('delete', u)">Delete</button>
                  </div>
                </td>
              </tr>
              <tr v-if="users.length === 0"><td colspan="6" class="px-3 py-6 text-center text-zinc-500">No users.</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="space-y-3">
        <h2 class="text-base font-semibold text-white">Apps <span class="text-sm font-normal text-zinc-500">({{ allApps.length }})</span></h2>
        <div :class="[card, 'overflow-x-auto']">
          <table class="w-full min-w-[720px] text-left text-sm text-zinc-300">
            <thead><tr><th :class="th">App</th><th :class="th">Owner</th><th :class="th">Status</th><th :class="th">Resources</th><th :class="th"><span class="sr-only">Actions</span></th></tr></thead>
            <tbody>
              <tr v-for="a in allApps" :key="a.id" class="border-t border-pico-border">
                <td :class="td">
                  <div class="font-medium text-white">{{ a.name }}</div>
                  <div class="break-all font-mono text-xs text-zinc-500">{{ a.hostname }}</div>
                </td>
                <td :class="[td, 'break-all']">{{ a.owner_email }}</td>
                <td :class="td"><StatusBadge :status="a.status" /></td>
                <td :class="td">
                  <template v-if="a.resources">
                    <div class="text-xs">{{ resSummary(a) }}</div>
                    <div class="mt-0.5 flex gap-1 text-xs">
                      <span v-if="a.resources.overridden" class="text-accent-light">overridden</span>
                      <span v-else class="text-zinc-600">default</span>
                      <span v-if="a.resources.exceeds_platform_caps" class="text-amber-400">exceeds caps</span>
                    </div>
                  </template>
                  <span v-else class="text-zinc-600">-</span>
                </td>
                <td :class="[td, 'whitespace-nowrap text-right']">
                  <div class="flex justify-end gap-1.5">
                    <button :class="btn" @click="resError = ''; resApp = a">Edit resources</button>
                    <button v-if="a.status === 'suspended'" :class="btn" @click="act(() => unsuspendApp(a.id))">Unsuspend</button>
                    <button v-else :class="btn" @click="askApp('suspend', a)">Suspend</button>
                    <button :class="btnDanger" @click="askApp('delete', a)">Delete</button>
                  </div>
                </td>
              </tr>
              <tr v-if="allApps.length === 0"><td colspan="5" class="px-3 py-6 text-center text-zinc-500">No apps yet.</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="space-y-3">
        <h2 class="text-base font-semibold text-white">DB servers <span class="text-sm font-normal text-zinc-500">({{ servers.length }})</span></h2>
        <div :class="[card, 'overflow-x-auto']">
          <table class="w-full min-w-[560px] text-left text-sm text-zinc-300">
            <thead><tr><th :class="th">Name</th><th :class="th">Host</th><th :class="th">Capacity</th><th :class="th">Status</th></tr></thead>
            <tbody>
              <tr v-for="s in servers" :key="s.id" class="border-t border-pico-border">
                <td :class="td">{{ s.name }}</td>
                <td :class="[td, 'break-all font-mono text-xs']">{{ s.host }}:{{ s.port }}</td>
                <td :class="td"><DbCapacityBar :server="s" /></td>
                <td :class="td">
                  <select :value="s.status" aria-label="Server status" :class="[inputSm, 'w-28']" @change="patch(s, { status: ($event.target as HTMLSelectElement).value })">
                    <option>active</option>
                    <option>draining</option>
                    <option>full</option>
                  </select>
                </td>
              </tr>
              <tr v-if="servers.length === 0"><td colspan="4" class="px-3 py-6 text-center text-zinc-500">No DB servers. Add one below.</td></tr>
            </tbody>
          </table>
        </div>

        <form :class="[card, 'space-y-5 p-4 sm:p-5']" @submit.prevent="addServer">
          <h3 class="text-sm font-semibold text-zinc-200">Add DB server</h3>
          <label :class="label">
            <span :class="labelText">Name</span>
            <input v-model="form.name" required placeholder="primary" :class="input" />
            <span>Label shown in the admin UI.</span>
          </label>
          <label :class="label">
            <span :class="labelText">Host</span>
            <input v-model="form.host" required placeholder="private-xxxx.db.ondigitalocean.com" :class="[input, 'font-mono']" />
            <span>Hostname the platform connects to. For DO managed MySQL use the private host.</span>
          </label>
          <div class="grid grid-cols-1 gap-5 sm:grid-cols-2">
            <label :class="label">
              <span :class="labelText">Port</span>
              <input v-model.number="form.port" required type="text" inputmode="numeric" pattern="[0-9]{1,5}" placeholder="25060" :class="input" />
              <span>25060 for DO managed, 3306 in-cluster.</span>
            </label>
            <label :class="label">
              <span :class="labelText">Max databases</span>
              <input v-model.number="form.max_databases" required type="text" inputmode="numeric" pattern="[0-9]{1,4}" placeholder="25" :class="input" />
              <span>Databases placed here before it counts as full (~25 for 1 GiB).</span>
            </label>
          </div>
          <label :class="label">
            <span :class="labelText">Admin user</span>
            <input v-model="form.admin_user" required placeholder="doadmin" autocomplete="off" :class="input" />
            <span>Account used to create each app's database and user. Never root.</span>
          </label>
          <label :class="label">
            <span :class="labelText">Admin password</span>
            <input v-model="form.admin_password" required type="password" autocomplete="new-password" :class="input" />
            <span>Stored encrypted; never shown again.</span>
          </label>
          <div class="flex justify-end">
            <button type="submit" :disabled="saving" class="rounded-md bg-accent-dark px-4 py-2 text-sm font-semibold text-white hover:bg-accent disabled:opacity-50">
              {{ saving ? 'Adding…' : 'Add DB server' }}
            </button>
          </div>
        </form>
      </section>

      <section class="space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h2 class="text-base font-semibold text-white">Audit log <span class="text-sm font-normal text-zinc-500">(last 100)</span></h2>
          <input v-model="auditFilter" placeholder="filter by action or actor" aria-label="Filter audit log" :class="[inputSm, 'w-full sm:w-64']" />
        </div>
        <div :class="[card, 'max-h-96 overflow-auto']">
          <table class="w-full min-w-[640px] text-left text-xs text-zinc-300">
            <thead class="sticky top-0 bg-pico-panel"><tr><th :class="th">At</th><th :class="th">Actor</th><th :class="th">Action</th><th :class="th">Target</th><th :class="th">Detail</th></tr></thead>
            <tbody>
              <tr v-for="a in filteredAudit" :key="a.id" class="border-t border-pico-border">
                <td class="whitespace-nowrap px-3 py-2">{{ a.at }}</td>
                <td class="px-3 py-2">{{ a.actor_email ?? '-' }}</td>
                <td class="px-3 py-2 font-mono">{{ a.action }}</td>
                <td class="px-3 py-2">{{ a.target_type }} {{ a.target_id }}</td>
                <td class="px-3 py-2 text-zinc-400">{{ a.detail }}</td>
              </tr>
              <tr v-if="filteredAudit.length === 0"><td colspan="5" class="px-3 py-6 text-center text-zinc-500">No entries.</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </main>

    <ResourcesDialog :app="resApp" :error="resError" @save="saveResources" @cancel="resApp = null" />
    <ConfirmDialog
      :open="confirm !== null"
      :title="confirm?.title ?? ''"
      :message="confirm?.message ?? ''"
      :confirm-label="confirm?.label"
      danger
      @confirm="runConfirm"
      @cancel="confirm = null"
    />
  </div>
</template>
