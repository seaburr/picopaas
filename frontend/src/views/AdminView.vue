<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, useId } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import NavBar from '../components/NavBar.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DbCapacityBar from '../components/DbCapacityBar.vue'
import StatusBadge from '../components/StatusBadge.vue'
import ResourcesDialog from '../components/ResourcesDialog.vue'
import {
  approveUser, createDbServer, deleteAdminApp, deleteUser, listAllApps, listAudit, listDbServers, listUsers,
  patchAppResources, patchDbServer, resetUserPassword, setAppLimit, suspendApp, suspendUser, unsuspendApp, unsuspendUser,
} from '../api/admin'
import { ApiError } from '../api/client'
import { useModal } from '../composables/useModal'
import { useToastStore } from '../stores/toast'
import { useAuthStore } from '../stores/auth'
import type { AdminApp, AdminUser, AuditEntry, DbServer, ResourcesPatch } from '../types'
import { copyText, fullDate, humanDate, stamp, stampTitle } from '../utils/format'
import { Ban, Check, KeyRound, Play, SlidersHorizontal, Trash2, X } from 'lucide-vue-next'
import IconButton from '../components/IconButton.vue'

const route = useRoute()
const router = useRouter()
const toast = useToastStore()
const auth = useAuthStore()
const maxApps = computed(() => auth.user?.max_apps ?? 5)
const SERVER_STATUS_HELP: Record<string, string> = {
  active: 'Active: new databases can be placed on this server.',
  draining: 'Draining: no new databases go here; existing ones keep working. Use it before retiring a server.',
  full: 'Full: same as Draining (legacy status). A server at its max databases is skipped automatically anyway.',
}
const uid = useId()

const users = ref<AdminUser[]>([])
const servers = ref<DbServer[]>([])
const allApps = ref<AdminApp[]>([])
const audit = ref<AuditEntry[]>([])
const loaded = ref(false)
const error = ref('')
const msg = (e: unknown, fallback: string) => (e instanceof ApiError ? e.message : fallback)

// Tabs live in ?tab= so they are linkable.
const TABS = ['users', 'apps', 'servers', 'audit'] as const
type Tab = (typeof TABS)[number]
const tab = computed<Tab>(() => (TABS.includes(route.query.tab as Tab) ? (route.query.tab as Tab) : 'users'))
const q = ref('')
function setTab(t: Tab) {
  q.value = ''
  router.replace({ path: '/admin', query: t === 'users' ? {} : { tab: t } })
}
function onTabKey(e: KeyboardEvent) {
  const i = TABS.indexOf(tab.value)
  const n = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : -1
  if (n < 0) return
  const t = TABS[(n + TABS.length) % TABS.length]
  setTab(t)
  document.getElementById(`${uid}-tab-${t}`)?.focus()
}

const match = (q: string, ...fields: (string | null | undefined)[]) => {
  const s = q.trim().toLowerCase()
  return !s || fields.some((f) => (f ?? '').toLowerCase().includes(s))
}
const pendingUsers = computed(() => users.value.filter((u) => u.status === 'pending' && match(q.value, u.email)))
const otherUsers = computed(() => users.value.filter((u) => u.status !== 'pending' && match(q.value, u.email)))
const pendingCount = computed(() => users.value.filter((u) => u.status === 'pending').length)
const filteredApps = computed(() => allApps.value.filter((a) => match(q.value, a.name, a.hostname, a.owner_email, a.image)))
const filteredAudit = computed(() => audit.value.filter((a) => match(q.value, a.action, a.actor_email, a.target_type, String(a.target_id ?? ''), a.detail)))
const warnServers = computed(() => servers.value.filter((s) => s.warn))

async function load() {
  try {
    ;[users.value, servers.value, allApps.value, audit.value] = await Promise.all([
      listUsers(), listDbServers(), listAllApps(), listAudit(100),
    ])
    for (const u of users.value) limits[u.id] = u.app_limit_effective
    error.value = ''
  } catch (e) {
    error.value = msg(e, "Couldn't load admin data. Reload to try again.")
  } finally {
    loaded.value = true
  }
}
onMounted(load)

async function act(fn: () => Promise<unknown>, ok: string, fail: string) {
  error.value = ''
  try {
    await fn()
    toast.success(ok)
  } catch (e) {
    error.value = `${fail}: ${msg(e, 'try again')}`
  }
  await load()
}

// ---- confirms (type-the-name for deletes) ----
type Confirm = { title: string; message: string; label: string; require?: string; run: () => Promise<void> }
const confirm = ref<Confirm | null>(null)
const confirmBusy = ref(false)
async function runConfirm() {
  const c = confirm.value
  if (!c) return
  confirmBusy.value = true
  await c.run()
  confirmBusy.value = false
  confirm.value = null
  if (pendingTemp) {
    // open the result dialog only after the confirm dialog has closed (one modal at a time)
    await nextTick()
    tempPw.value = pendingTemp
    pendingTemp = null
  }
}
function askUser(kind: 'suspend' | 'delete' | 'reject' | 'reset', u: AdminUser) {
  if (kind === 'suspend')
    confirm.value = { title: `Suspend ${u.email}`, message: "This stops all of their apps and blocks them from the platform. Their data is kept; unsuspend to restore.", label: 'Suspend user', run: () => act(() => suspendUser(u.id), `${u.email} suspended.`, "Couldn't suspend the user") }
  else if (kind === 'reject')
    confirm.value = { title: `Reject ${u.email}`, message: 'This removes their request. They can request access again.', label: 'Reject request', run: () => act(() => deleteUser(u.id), `${u.email} rejected.`, "Couldn't reject the request") }
  else if (kind === 'reset')
    confirm.value = { title: `Reset password for ${u.email}`, message: 'This generates a temporary password. Their current password stops working immediately. Give them the new one; they should change it right away.', label: 'Reset password', run: () => resetPassword(u) }
  else
    confirm.value = { title: `Delete ${u.email}`, message: 'This permanently deletes the user and all of their apps, databases and storage. This cannot be undone.', label: 'Delete user and data', require: u.email, run: () => act(() => deleteUser(u.id), `${u.email} deleted.`, "Couldn't delete the user") }
}
function askApp(kind: 'suspend' | 'delete', a: AdminApp) {
  confirm.value =
    kind === 'suspend'
      ? { title: `Suspend ${a.name}`, message: "The app is stopped. Its owner can't edit or restart it until you unsuspend it.", label: 'Suspend app', run: () => act(() => suspendApp(a.id), `${a.name} suspended.`, "Couldn't suspend the app") }
      : { title: `Delete ${a.name}`, message: `Permanently deletes ${a.name}, its database and its storage bucket. This cannot be undone.`, label: 'Delete app', require: a.name, run: () => act(() => deleteAdminApp(a.id), `${a.name} deleted.`, "Couldn't delete the app") }
}

// ---- approve ----
const approving = ref<number | null>(null)
async function approve(u: AdminUser) {
  approving.value = u.id
  await act(() => approveUser(u.id), `${u.email} approved.`, "Couldn't approve the user")
  approving.value = null
}

// ---- reset password: result dialog shows the temp password once ----
const tempPw = ref<{ email: string; password: string } | null>(null)
let pendingTemp: { email: string; password: string } | null = null
const pwDlg = ref<HTMLDialogElement | null>(null)
const pwModal = useModal(pwDlg, () => tempPw.value !== null, () => (tempPw.value = null))
async function resetPassword(u: AdminUser) {
  try {
    const r = await resetUserPassword(u.id)
    pendingTemp = { email: u.email, password: r.temporary_password }
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) error.value = "Password reset isn't available on this server yet."
    else error.value = `Couldn't reset the password: ${msg(e, 'try again')}`
  }
}
async function copyTemp() {
  if (tempPw.value && (await copyText(tempPw.value.password))) toast.success('Copied.', 2000)
  else toast.error("Couldn't copy. Select the password and copy it manually.")
}

// ---- app limit with inline saved check ----
const limits = reactive<Record<number, number>>({})
const saved = reactive<Record<number, boolean>>({})
async function saveLimit(u: AdminUser) {
  error.value = ''
  try {
    if (!Number.isInteger(limits[u.id]) || limits[u.id] < 0 || limits[u.id] > maxApps.value) {
      error.value = `App limit must be a whole number from 0 to ${maxApps.value} (the platform max).`
      return
    }
    const updated = await setAppLimit(u.id, limits[u.id])
    users.value = users.value.map((x) => (x.id === u.id ? { ...x, ...updated } : x))
    saved[u.id] = true
    setTimeout(() => (saved[u.id] = false), 2000)
  } catch (e) {
    error.value = `Couldn't update the app limit: ${msg(e, 'try again')}`
  }
}

// ---- Database servers ----
async function patch(s: DbServer, payload: { status?: string; max_databases?: number }) {
  error.value = ''
  try {
    const updated = await patchDbServer(s.id, payload)
    servers.value = servers.value.map((x) => (x.id === s.id ? updated : x))
    toast.success(`${s.name} set to ${updated.status}.`)
  } catch (e) {
    error.value = `Couldn't update the database server: ${msg(e, 'try again')}`
    await load()
  }
}
const emptyForm = () => ({ name: '', host: '', port: 3306, admin_user: '', admin_password: '', max_databases: 25 })
const form = reactive(emptyForm())
const addOpen = ref(false)
const addDlg = ref<HTMLDialogElement | null>(null)
const addModal = useModal(addDlg, () => addOpen.value, () => !saving.value && (addOpen.value = false))
const saving = ref(false)
const addError = ref('')
function openAdd() {
  Object.assign(form, emptyForm())
  addError.value = ''
  addOpen.value = true
}
async function addServer() {
  addError.value = ''
  saving.value = true
  try {
    await createDbServer({ ...form })
    Object.assign(form, emptyForm()) // clears the password
    addOpen.value = false
    toast.success('Database server added. Apps can now request a MySQL database.')
    await load()
  } catch (e) {
    addError.value = msg(e, "Couldn't add the database server. Check the host and credentials.")
  } finally {
    saving.value = false
  }
}

// ---- app resources ----
const resApp = ref<AdminApp | null>(null)
const resError = ref('')
async function saveResources(body: ResourcesPatch) {
  if (!resApp.value) return
  resError.value = ''
  try {
    await patchAppResources(resApp.value.id, body)
    toast.success(`Resources updated for ${resApp.value.name}.`)
    resApp.value = null
    await load()
  } catch (e) {
    resError.value = msg(e, "Couldn't update resources. Try again.")
  }
}
const resSummary = (a: AdminApp) =>
  a.resources ? `${a.resources.cpu_request_m}-${a.resources.cpu_limit_m}m · ${a.resources.mem_request_mi}-${a.resources.mem_limit_mi}Mi` : ''

const rowBase = 'row-pad flex flex-col gap-1 text-sm text-zinc-200'
// One fixed template per list, shared by the header and every row (md+; apps/audit lg+). Only the first/detail column flexes.
const usersGrid = 'md:grid md:grid-cols-[minmax(0,1fr)_7rem_3.5rem_6rem_6.5rem] md:items-center md:gap-3'
const appsGrid = 'lg:grid lg:grid-cols-[minmax(0,1fr)_12rem_6.5rem_10.5rem_6.5rem] lg:items-center lg:gap-3'
const serversGrid = 'md:grid md:grid-cols-[8rem_minmax(0,1fr)_9rem_8.5rem] md:items-center md:gap-3'
const auditGrid = 'lg:grid lg:grid-cols-[8rem_12rem_9.5rem_8rem_minmax(0,1fr)] lg:items-start lg:gap-3'
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main id="main" tabindex="-1" class="mx-auto max-w-5xl space-y-3 px-4 py-4 sm:px-6">
      <h1 class="page-title">Admin</h1>

      <div v-if="error" class="alert-error" role="alert">{{ error }}</div>

      <div v-if="warnServers.length" class="alert-warn flex flex-wrap items-center justify-between gap-2" role="status">
        <span>Database capacity is above 80% on {{ warnServers.map((s) => s.name).join(', ') }}. Add a database server or raise the limit.</span>
        <button type="button" class="btn-secondary btn-sm" @click="setTab('servers')">Go to database servers</button>
      </div>

      <section v-if="loaded && servers.length === 0" class="card card-pad border-accent/40" aria-labelledby="setup-title">
        <h2 id="setup-title" class="section-title">Set up your platform</h2>
        <p class="help">Finish these so users can deploy apps with databases.</p>
        <ol class="mt-3 space-y-2 text-sm text-zinc-200">
          <li class="flex flex-wrap items-center gap-2">
            <span class="badge-warn">To do</span>
            <span>Add a MySQL database server. Until then the Database option is hidden from users.</span>
            <button type="button" class="btn-primary btn-sm" @click="setTab('servers'); openAdd()">Add database server</button>
          </li>
          <li class="flex flex-wrap items-center gap-2">
            <span :class="pendingCount ? 'badge-warn' : 'badge-neutral'">{{ pendingCount ? 'To do' : 'Nothing pending' }}</span>
            <span>Approve access requests{{ pendingCount ? ` (${pendingCount} waiting)` : '' }}.</span>
            <button v-if="pendingCount" type="button" class="btn-secondary btn-sm" @click="setTab('users')">Review</button>
          </li>
          <li class="flex flex-wrap items-center gap-2">
            <span class="badge-neutral">Optional</span>
            <span>Check cluster capacity on the <RouterLink to="/admin/health" class="link">Health</RouterLink> page.</span>
          </li>
        </ol>
      </section>

      <div role="tablist" aria-label="Admin sections" class="flex gap-1 overflow-x-auto border-b border-pico-border" @keydown="onTabKey">
        <button
          v-for="t in [{ id: 'users', label: 'Users', n: users.length }, { id: 'apps', label: 'Apps', n: allApps.length }, { id: 'servers', label: 'Database servers', n: servers.length }, { id: 'audit', label: 'Audit', n: null }] as const"
          :id="`${uid}-tab-${t.id}`"
          :key="t.id"
          type="button"
          role="tab"
          :aria-selected="tab === t.id"
          :aria-controls="`${uid}-panel`"
          :tabindex="tab === t.id ? 0 : -1"
          class="-mb-px inline-flex min-h-[36px] items-center gap-1.5 whitespace-nowrap border-b-2 px-3 text-sm"
          :class="tab === t.id ? 'border-accent-light font-medium text-white' : 'border-transparent text-zinc-300 hover:text-white'"
          @click="setTab(t.id)"
        >
          {{ t.label }}
          <span v-if="t.n !== null" class="meta">{{ t.n }}</span>
          <span v-if="t.id === 'users' && pendingCount" class="badge-warn">{{ pendingCount }} pending</span>
        </button>
      </div>

      <div :id="`${uid}-panel`" role="tabpanel" :aria-labelledby="`${uid}-tab-${tab}`" class="space-y-3">
        <div v-if="tab !== 'servers'">
          <label :for="`${uid}-q`" class="sr-only">Search</label>
          <input :id="`${uid}-q`" v-model="q" type="search" class="input sm:max-w-xs" :placeholder="tab === 'users' ? 'Search users' : tab === 'apps' ? 'Search apps, owners, images' : 'Filter by action, actor, target'" />
        </div>

        <!-- USERS -->
        <template v-if="tab === 'users'">
          <section v-if="pendingUsers.length" aria-label="Waiting for approval" class="space-y-1.5">
            <h2 class="section-title text-amber-300">Waiting for approval ({{ pendingUsers.length }})</h2>
            <ul class="card divide-y divide-pico-border border-amber-500/40">
              <li v-for="u in pendingUsers" :key="u.id" :class="[rowBase, 'md:flex-row md:items-center md:justify-between md:gap-3']">
                <div class="min-w-0">
                  <div class="truncate font-medium text-white" :title="u.email">{{ u.email }}</div>
                  <div class="meta" :title="fullDate(u.created_at)">Requested {{ humanDate(u.created_at) }} · app limit {{ u.app_limit_effective }}</div>
                </div>
                <div class="flex shrink-0 gap-1">
                  <IconButton :icon="Check" :label="approving === u.id ? 'Approving…' : `Approve ${u.email}`" :disabled="approving === u.id" @click="approve(u)" />
                  <IconButton :icon="X" :label="`Reject ${u.email}`" danger @click="askUser('reject', u)" />
                </div>
              </li>
            </ul>
          </section>

          <div class="card overflow-hidden">
            <div :class="[usersGrid, 'table-th row-pad hidden border-b border-pico-border']">
              <span>User</span><span>Status</span><span class="text-right">Apps</span><span>Limit</span><span class="text-right">Actions</span>
            </div>
            <ul class="divide-y divide-pico-border">
              <li v-for="u in otherUsers" :key="u.id" :class="[rowBase, usersGrid]">
                <div class="min-w-0">
                  <div class="flex min-w-0 items-center gap-1.5">
                    <span class="truncate font-medium text-white" :title="u.email">{{ u.email }}</span>
                    <span v-if="u.role === 'platform_admin'" class="badge-info shrink-0">Platform admin</span>
                  </div>
                  <div class="meta" :title="fullDate(u.created_at)">Joined {{ humanDate(u.created_at) }}</div>
                </div>
                <div><StatusBadge :status="u.status" /></div>
                <div class="mono md:text-right"><span class="meta md:hidden">Apps </span>{{ u.app_count }}<span class="meta"> / {{ u.app_limit_effective }}</span></div>
                <div v-if="u.role === 'platform_admin'" class="mono text-sm" :title="`Platform admins use the platform max (${maxApps})`">
                  <span class="meta md:hidden">Limit </span>{{ u.app_limit_effective }}<span class="meta"> (platform max)</span>
                </div>
                <form v-else class="flex h-7 items-center gap-1" @submit.prevent="saveLimit(u)">
                  <span class="meta md:hidden">Limit</span>
                  <input v-model.number="limits[u.id]" type="text" inputmode="numeric" pattern="[0-9]*" :max="maxApps" :aria-label="`App limit for ${u.email}`" class="input mono !min-h-[28px] w-14 !px-2 !py-0.5" />
                  <button v-if="limits[u.id] !== u.app_limit_effective" type="submit" class="btn-icon btn-icon-sm text-accent-light" :aria-label="`Save app limit for ${u.email}`" title="Save limit"><Check :size="16" aria-hidden="true" /></button>
                  <span v-else-if="saved[u.id]" class="text-emerald-300" role="status" title="Saved"><Check :size="16" aria-hidden="true" /><span class="sr-only">Saved</span></span>
                  <span class="meta" :title="`The platform max is ${maxApps} apps per account`">of {{ maxApps }} max</span>
                </form>
                <div class="flex gap-0.5 md:justify-end">
                  <template v-if="u.role !== 'platform_admin'">
                    <IconButton v-if="u.status === 'suspended'" :icon="Play" :label="`Unsuspend ${u.email}`" sm @click="act(() => unsuspendUser(u.id), `${u.email} unsuspended.`, 'Couldn\'t unsuspend the user')" />
                    <IconButton v-if="u.status === 'active'" :icon="Ban" :label="`Suspend ${u.email}`" sm @click="askUser('suspend', u)" />
                    <IconButton :icon="KeyRound" :label="`Reset password for ${u.email}`" sm @click="askUser('reset', u)" />
                    <IconButton :icon="Trash2" :label="`Delete ${u.email}`" sm danger @click="askUser('delete', u)" />
                  </template>
                </div>
              </li>
              <li v-if="loaded && otherUsers.length === 0" class="px-3 py-4 text-center text-sm text-zinc-400">{{ q ? 'No users match your search.' : 'No other users yet.' }}</li>
            </ul>
          </div>
        </template>

        <!-- APPS -->
        <div v-else-if="tab === 'apps'" class="card overflow-hidden">
          <div :class="[appsGrid, 'table-th row-pad hidden border-b border-pico-border']">
            <span>App</span><span>Owner</span><span>Status</span><span>Resources</span><span class="text-right">Actions</span>
          </div>
          <ul class="divide-y divide-pico-border">
            <li v-for="a in filteredApps" :key="a.id" :class="[rowBase, appsGrid]">
              <div class="min-w-0">
                <div class="truncate font-medium text-white" :title="a.name">{{ a.name }}</div>
                <div class="mono truncate text-zinc-400" :title="a.hostname">{{ a.hostname }}</div>
              </div>
              <div class="mono truncate text-zinc-300" :title="a.owner_email"><span class="meta lg:hidden">Owner: </span>{{ a.owner_email }}</div>
              <div><StatusBadge :status="a.status" /></div>
              <div>
                <template v-if="a.resources">
                  <div class="mono truncate" :title="resSummary(a)">{{ resSummary(a) }}</div>
                  <div class="mono flex gap-2">
                    <span v-if="a.resources.overridden" class="text-accent-light">overridden</span>
                    <span v-else class="text-zinc-400">default</span>
                    <span v-if="a.resources.exceeds_platform_caps" class="text-amber-300">exceeds caps</span>
                  </div>
                </template>
                <span v-else class="text-zinc-400">-</span>
              </div>
              <div class="flex gap-0.5 lg:justify-end">
                <IconButton :icon="SlidersHorizontal" :label="`Edit resources for ${a.name}`" sm @click="resError = ''; resApp = a" />
                <IconButton v-if="a.status === 'suspended'" :icon="Play" :label="`Unsuspend ${a.name}`" sm @click="act(() => unsuspendApp(a.id), `${a.name} unsuspended.`, 'Couldn\'t unsuspend the app')" />
                <IconButton v-else :icon="Ban" :label="`Suspend ${a.name}`" sm @click="askApp('suspend', a)" />
                <IconButton :icon="Trash2" :label="`Delete ${a.name}`" sm danger @click="askApp('delete', a)" />
              </div>
            </li>
            <li v-if="loaded && filteredApps.length === 0" class="px-3 py-4 text-center text-sm text-zinc-400">{{ q ? 'No apps match your search.' : 'No apps yet.' }}</li>
          </ul>
        </div>

        <!-- DB SERVERS -->
        <template v-else-if="tab === 'servers'">
          <div class="flex items-center justify-between gap-2">
            <p class="text-sm text-zinc-300">MySQL servers that app databases are placed on.</p>
            <button type="button" class="btn-primary btn-sm shrink-0" @click="openAdd">Add server</button>
          </div>
          <div class="card overflow-hidden">
            <div :class="[serversGrid, 'table-th row-pad hidden border-b border-pico-border']">
              <span>Name</span><span>Host</span><span>Capacity</span><span>Status</span>
            </div>
            <ul class="divide-y divide-pico-border">
              <li v-for="s in servers" :key="s.id" :class="[rowBase, serversGrid]">
                <div class="truncate font-medium text-white" :title="s.name">{{ s.name }}</div>
                <div class="mono truncate text-zinc-300" :title="`${s.host}:${s.port}`">{{ s.host }}:{{ s.port }}</div>
                <DbCapacityBar :server="s" />
                <select
                  :value="s.status"
                  :aria-label="`Status of ${s.name}`"
                  :title="SERVER_STATUS_HELP[s.status] ?? ''"
                  class="input !min-h-[28px] w-full !py-0.5 !pl-2"
                  @change="patch(s, { status: ($event.target as HTMLSelectElement).value })"
                >
                  <option value="active">Active</option>
                  <option value="draining">Draining</option>
                  <option v-if="s.status === 'full'" value="full">Full (legacy)</option>
                </select>
              </li>
              <li v-if="loaded && servers.length === 0" class="px-3 py-5 text-center">
                <p class="text-sm text-zinc-300">No database servers yet. Users can't create apps with a database until you add one.</p>
                <button type="button" class="btn-primary mt-3" @click="openAdd">Add server</button>
              </li>
            </ul>
          </div>
          <p v-if="servers.length" class="help mt-2">
            <strong class="text-zinc-200">Status</strong> controls where <em>new</em> app databases go. <em>Active</em>
            servers take them (the one with the most free slots wins; a server at its max is skipped automatically).
            <em>Draining</em> takes none; databases already on it keep working and are never moved. Drain a server
            before retiring it. If no server can take a new database, users can't tick Database on new apps.
          </p>
        </template>

        <!-- AUDIT -->
        <div v-else class="card max-h-[34rem] overflow-auto">
          <div :class="[auditGrid, 'table-th row-pad sticky top-0 hidden border-b border-pico-border bg-pico-panel']">
            <span>When</span><span>Actor</span><span>Action</span><span>Target</span><span>Detail</span>
          </div>
          <ul class="divide-y divide-pico-border">
            <li v-for="a in filteredAudit" :key="a.id" :class="[rowBase, auditGrid, 'text-zinc-200']">
              <span class="mono text-zinc-300" :title="stampTitle(a.at)">{{ stamp(a.at) }}</span>
              <span class="mono truncate" :title="a.actor_email ?? undefined">{{ a.actor_email ?? '-' }}</span>
              <span class="mono truncate" :title="a.action">{{ a.action }}</span>
              <span class="mono truncate" :title="`${a.target_type ?? ''} ${a.target_id ?? ''}`">{{ a.target_type }} {{ a.target_id }}</span>
              <span class="break-words text-xs leading-4 text-zinc-300">{{ a.detail }}</span>
            </li>
            <li v-if="loaded && filteredAudit.length === 0" class="px-3 py-4 text-center text-sm text-zinc-400">No entries.</li>
          </ul>
        </div>
      </div>
    </main>

    <!-- Add database server -->
    <dialog
      ref="addDlg"
      :aria-labelledby="`${uid}-add-title`"
      class="m-auto max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-lg overflow-y-auto rounded-lg border border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/70"
      @cancel="addModal.onNativeCancel"
      @click="addModal.onBackdrop"
    >
      <form v-if="addOpen" class="space-y-3 p-4" @submit.prevent="addServer">
        <h2 :id="`${uid}-add-title`" class="section-title">Add database server</h2>
        <div>
          <label :for="`${uid}-n`" class="label">Name</label>
          <input :id="`${uid}-n`" v-model="form.name" required placeholder="primary" class="input" data-autofocus />
          <p class="help">Label shown in this page.</p>
        </div>
        <div>
          <label :for="`${uid}-h`" class="label">Host</label>
          <input :id="`${uid}-h`" v-model="form.host" required placeholder="private-xxxx.db.ondigitalocean.com" class="input mono" />
          <p class="help">Hostname the platform connects to. For DO managed MySQL use the private host.</p>
        </div>
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <label :for="`${uid}-p`" class="label">Port</label>
            <input :id="`${uid}-p`" v-model.number="form.port" required type="text" inputmode="numeric" pattern="[0-9]{1,5}" placeholder="25060" class="input mono" />
            <p class="help">25060 for DO managed, 3306 in-cluster.</p>
          </div>
          <div>
            <label :for="`${uid}-m`" class="label">Max databases</label>
            <input :id="`${uid}-m`" v-model.number="form.max_databases" required type="text" inputmode="numeric" pattern="[0-9]{1,4}" placeholder="25" class="input" />
            <p class="help">How many app databases this server may hold before it counts as full.</p>
          </div>
        </div>
        <div>
          <label :for="`${uid}-u`" class="label">Admin user</label>
          <input :id="`${uid}-u`" v-model="form.admin_user" required placeholder="doadmin" autocomplete="off" class="input" />
          <p class="help">Account used to create each app's database and user. Never root.</p>
        </div>
        <div>
          <label :for="`${uid}-pw`" class="label">Admin password</label>
          <input :id="`${uid}-pw`" v-model="form.admin_password" required type="password" autocomplete="new-password" class="input" />
          <p class="help">Stored encrypted; never shown again.</p>
        </div>
        <div v-if="addError" class="alert-error" role="alert">{{ addError }}</div>
        <div class="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button type="button" class="btn-secondary" :disabled="saving" @click="addOpen = false">Cancel</button>
          <button type="submit" class="btn-primary" :disabled="saving">{{ saving ? 'Adding…' : 'Add database server' }}</button>
        </div>
      </form>
    </dialog>

    <!-- Temporary password, shown once -->
    <dialog
      ref="pwDlg"
      :aria-labelledby="`${uid}-pw-title`"
      class="m-auto w-[calc(100%-2rem)] max-w-md rounded-lg border border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/70"
      @cancel="pwModal.onNativeCancel"
      @click="pwModal.onBackdrop"
    >
      <div v-if="tempPw" class="p-5">
        <h2 :id="`${uid}-pw-title`" class="section-title">Temporary password</h2>
        <p class="mt-2 text-sm text-zinc-300">
          Give this to <strong class="break-all text-white">{{ tempPw.email }}</strong>. It is shown once and can't be retrieved later. They should change it on the Account page.
        </p>
        <div class="mt-3 flex items-stretch gap-2">
          <code class="min-w-0 flex-1 select-all overflow-x-auto whitespace-nowrap rounded bg-pico-card px-3 py-2 text-sm text-accent-light">{{ tempPw.password }}</code>
          <button type="button" class="btn-primary shrink-0" data-autofocus @click="copyTemp">Copy</button>
        </div>
        <div class="mt-5 flex justify-end"><button type="button" class="btn-secondary" @click="tempPw = null">Done</button></div>
      </div>
    </dialog>

    <ResourcesDialog :app="resApp" :error="resError" @save="saveResources" @cancel="resApp = null" />
    <ConfirmDialog
      :open="confirm !== null"
      :title="confirm?.title ?? ''"
      :message="confirm?.message ?? ''"
      :confirm-label="confirm?.label"
      :require-text="confirm?.require"
      :busy="confirmBusy"
      danger
      @confirm="runConfirm"
      @cancel="confirm = null"
    />
  </div>
</template>
