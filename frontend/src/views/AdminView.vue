<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, useId, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PasswordField from '../components/PasswordField.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DbCapacityBar from '../components/DbCapacityBar.vue'
import StatusBadge from '../components/StatusBadge.vue'
import SettingsPanel from '../components/SettingsPanel.vue'
import ResourcesDialog from '../components/ResourcesDialog.vue'
import UserKeysDialog from '../components/UserKeysDialog.vue'
import BackupAdminDialog from '../components/BackupAdminDialog.vue'
import AdminProjects from '../components/AdminProjects.vue'
import AdminSuspensions from '../components/AdminSuspensions.vue'
import SuspendDialog from '../components/SuspendDialog.vue'
import {
  approveUser, createDbServer, deleteAdminApp, deleteUser, listAllApps, listSuspensions, downloadAuditLog, demoteUser, listAudit, promoteUser, listDbServers, listUsers,
  patchAppResources, patchDbServer, resetUserPassword, setAppLimit, setAppReplicas, suspendUser, unsuspendApp, unsuspendUser,
} from '../api/admin'
import { ApiError } from '../api/client'
import { dbEngineLabel } from '../composables/appUi'
import { useModal } from '../composables/useModal'
import { useToastStore } from '../stores/toast'
import { useAuthStore } from '../stores/auth'
import type { AdminApp, AdminUser, AuditEntry, DbServer, ResourcesPatch, Suspension } from '../types'
import { copyText, fullDate, humanDate, stamp, stampTitle } from '../utils/format'
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
const keysUser = ref<AdminUser | null>(null)
async function refreshUsers() {
  try {
    users.value = (await listUsers()) ?? []
  } catch { /* count stays stale */ }
}
async function refreshSuspensions() {
  try {
    suspensions.value = (await listSuspensions()) ?? []
  } catch { /* count stays stale */ }
}
const servers = ref<DbServer[]>([])
const allApps = ref<AdminApp[]>([])
const audit = ref<AuditEntry[]>([])
const suspensions = ref<Suspension[]>([])
const loaded = ref(false)
const error = ref('')
const msg = (e: unknown, fallback: string) => (e instanceof ApiError ? e.message : fallback)

// Tabs live in ?tab= so they are linkable.
const TABS = ['users', 'apps', 'projects', 'suspensions', 'servers', 'settings', 'audit'] as const
type Tab = (typeof TABS)[number]
const tab = computed<Tab>(() => (TABS.includes(route.query.tab as Tab) ? (route.query.tab as Tab) : 'users'))
// ?q= pre-fills the search (the Suspensions tab links to a user this way).
const q = ref(typeof route.query.q === 'string' ? route.query.q : '')
watch(() => route.query.q, (v) => { if (typeof v === 'string') q.value = v })
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
// Top-level suspensions: apps stopped by a project or user nest under it and are not counted.
const suspensionCount = computed(() => suspensions.value.filter((r) => !(r.type === 'app' && r.cause_id !== null)).length)
const warnServers = computed(() => servers.value.filter((s) => s.warn))

async function load() {
  try {
    ;[users.value, servers.value, allApps.value, audit.value, suspensions.value] = await Promise.all([
      listUsers(), listDbServers(), listAllApps(), listAudit(100), listSuspensions(),
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
async function exportAudit(format: 'csv' | 'json') {
  try {
    await downloadAuditLog(format)
  } catch (e) {
    toast.error(`Couldn't download the audit log: ${msg(e, 'try again')}`)
  }
}
// 409 detail ("would leave no active admin") goes to a toast; act() would put it in the page error banner.
async function setAdmin(u: AdminUser, promote: boolean) {
  try {
    await (promote ? promoteUser(u.id) : demoteUser(u.id))
    toast.success(promote ? `${u.email} is now a platform admin.` : `${u.email} is no longer a platform admin.`)
  } catch (e) {
    toast.error(msg(e, promote ? "Couldn't make admin" : "Couldn't remove admin"))
  }
  await load()
}
const activeAdmins = computed(() => users.value.filter((u) => u.role === 'platform_admin' && u.status === 'active').length)
function demoteBlocked(u: AdminUser) {
  return u.id === auth.user?.id && activeAdmins.value <= 1
}
function askAdmin(u: AdminUser, promote: boolean) {
  confirm.value = promote
    ? { title: `Make ${u.email} an admin`, message: 'They get full platform admin access: all users, apps, limits and audit logs.', label: 'Make admin', run: () => setAdmin(u, true) }
    : { title: `Remove admin from ${u.email}`, message: 'They become a regular user. Their apps are kept.', label: 'Remove admin', run: () => setAdmin(u, false) }
}
function askUser(kind: 'suspend' | 'delete' | 'reject' | 'reset', u: AdminUser) {
  if (kind === 'suspend') {
    susError.value = ''
    suspendTarget.value = u
  } else if (kind === 'reject')
    confirm.value = { title: `Reject ${u.email}`, message: 'This removes their request. They can request access again.', label: 'Reject request', run: () => act(() => deleteUser(u.id), `${u.email} rejected.`, "Couldn't reject the request") }
  else if (kind === 'reset')
    confirm.value = { title: `Reset password for ${u.email}`, message: 'This generates a temporary password. Their current password stops working immediately. Give them the new one; they should change it right away.', label: 'Reset password', run: () => resetPassword(u) }
  else
    confirm.value = { title: `Delete ${u.email}`, message: 'This permanently deletes the user and all of their apps, databases and blob storage. This cannot be undone.', label: 'Delete user and data', require: u.email, run: () => act(() => deleteUser(u.id), `${u.email} deleted.`, "Couldn't delete the user") }
}
// Suspending a single app is no longer offered here (suspend its project or user); the API stays.
function askDeleteApp(a: AdminApp) {
  confirm.value = { title: `Delete ${a.name}`, message: `Permanently deletes ${a.name}, its database and its blob storage bucket. This cannot be undone.`, label: 'Delete app', require: a.name, run: () => act(() => deleteAdminApp(a.id), `${a.name} deleted.`, "Couldn't delete the app") }
}

// ---- suspend user (reason required) ----
const suspendTarget = ref<AdminUser | null>(null)
const susBusy = ref(false)
const susError = ref('')
async function confirmSuspendUser(reason: string) {
  const u = suspendTarget.value
  if (!u) return
  susBusy.value = true
  susError.value = ''
  try {
    await suspendUser(u.id, reason)
    suspendTarget.value = null
    toast.success(`${u.email} suspended.`)
    await load()
  } catch (e) {
    susError.value = msg(e, "Couldn't suspend the user.")
  } finally {
    susBusy.value = false
  }
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
      error.value = 'App limit must be a whole number, and it can\'t be higher than the platform allows.'
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
// Change the admin password: the platform verifies it against the server before saving, 422 detail shown as a toast.
const pwServer = ref<DbServer | null>(null)
const pwNew = ref('')
const pwConfirm = ref('')
const pwSaving = ref(false)
const pwSrvDlg = ref<HTMLDialogElement | null>(null)
const pwSrvModal = useModal(pwSrvDlg, () => pwServer.value !== null, () => !pwSaving.value && (pwServer.value = null))
function openPw(s: DbServer) {
  pwNew.value = ''
  pwConfirm.value = ''
  pwServer.value = s
}
async function savePw() {
  const s = pwServer.value
  if (!s || pwNew.value !== pwConfirm.value) return
  pwSaving.value = true
  try {
    await patchDbServer(s.id, { admin_password: pwNew.value })
    pwServer.value = null
    toast.success(`Admin password for ${s.name} verified and saved.`)
  } catch (e) {
    toast.error(msg(e, "Couldn't change the admin password. Check it and try again."))
  } finally {
    pwNew.value = ''
    pwConfirm.value = ''
    pwSaving.value = false
  }
}
const emptyForm = () => ({ name: '', host: '', port: 3306, engine: 'mysql' as 'mysql' | 'pg', admin_user: '', admin_password: '', max_databases: 25 })
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
// Offer the engine's conventional managed port when switching engine, unless the admin has set a custom one.
const DEFAULT_PORT: Record<string, number> = { mysql: 3306, pg: 25060 }
watch(
  () => form.engine,
  (engine, prev) => {
    if (form.port === DEFAULT_PORT[prev]) form.port = DEFAULT_PORT[engine]
  },
)
async function addServer() {
  addError.value = ''
  saving.value = true
  try {
    await createDbServer({ ...form })
    Object.assign(form, emptyForm()) // clears the password
    addOpen.value = false
    toast.success(`Database server added. Apps can now request a ${dbEngineLabel(form.engine)} database.`)
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
function openLimits(a: AdminApp) {
  resError.value = ''
  resApp.value = a
}
const bkApp = ref<AdminApp | null>(null)
async function saveResources(body: ResourcesPatch, replicas: number | null) {
  if (!resApp.value) return
  resError.value = ''
  try {
    const r = await patchAppResources(resApp.value.id, body)
    if (replicas !== null) await setAppReplicas(resApp.value.id, replicas)
    toast.success(`Limits updated for ${resApp.value.name}.${r.restarts_app ? ' Pods are restarting.' : ''}`)
    resApp.value = null
    await load()
  } catch (e) {
    resError.value = msg(e, "Couldn't update limits. Try again.")
  }
}
// Open app: custom domain, then platform host, then the generated hostname.
const appHost = (a: AdminApp) => a.custom_domain || a.platform_host || a.hostname
const appUrl = (a: AdminApp) => `https://${appHost(a)}`
const resSummary = (a: AdminApp) =>
  `${a.resources.cpu_request_m}-${a.resources.cpu_limit_m}m · ${a.resources.mem_request_mi}-${a.resources.mem_limit_mi} MB`
// Compact list of the non-cpu/mem limits that differ from this app's platform defaults.
const overrideSummary = (a: AdminApp) => {
  const r = a.resources
  const d = r.defaults
  const gb = (n: number) => `${n} MB`
  const out: string[] = []
  if (a.replicas > 1) out.push(`x${a.replicas}`)
  if (a.volume) out.push(`vol ${a.volume.size_gi}G`)
  if (r.ephemeral_limit_mi !== d.ephemeral_limit_mi) out.push(`disk ${gb(r.ephemeral_limit_mi)}`)
  if (a.want_db && r.db_max_connections !== d.db_max_connections) out.push(`${r.db_max_connections} conns`)
  if (a.want_db && r.db_quota_mb !== d.db_quota_mb) out.push(`db ${gb(r.db_quota_mb)}`)
  if (a.want_storage && r.storage_quota_mb !== d.storage_quota_mb) out.push(`blob ${gb(r.storage_quota_mb)}`)
  if (r.egress_mbit !== d.egress_mbit) out.push(`${d.egress_mbit}→${r.egress_mbit} Mbit/s`)
  if (r.image_size_mb !== d.image_size_mb) out.push(`img ${gb(r.image_size_mb)}`)
  return out.join(' · ')
}

</script>

<template>
  <main id="main" tabindex="-1" class="page-main space-y-3">
      <h1 class="page-title">Admin</h1>

      <div v-if="error" class="alert-error" role="alert">{{ error }}</div>

      <div v-if="warnServers.length" class="alert-warn flex flex-wrap items-center justify-between gap-2" role="status">
        <span>Database capacity is above 80% on {{ warnServers.map((s) => s.name).join(', ') }}. Add a database server or raise the limit.</span>
        <button type="button" class="btn-secondary btn-sm" @click="setTab('servers')">Go to database servers</button>
      </div>

      <section v-if="loaded && servers.length === 0" class="sec pt-2" aria-labelledby="setup-title">
        <h2 id="setup-title" class="section-title">Set up your platform</h2>
        <p class="help">Finish these so users can deploy apps with databases.</p>
        <ol class="mt-2 text-sm text-zinc-200">
          <li class="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-pico-border py-1.5">
            <span class="st tone-warn w-28"><span class="dot dot-warn" aria-hidden="true" />To do</span>
            <span>Add a database server (MySQL or PostgreSQL). Until then the Database option is hidden from users.</span>
            <button type="button" class="btn-primary btn-sm" @click="setTab('servers'); openAdd()">Add database server</button>
          </li>
          <li class="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-pico-border py-1.5">
            <span class="st w-28" :class="pendingCount ? 'tone-warn' : 'tone-off'"><span class="dot" :class="pendingCount ? 'dot-warn' : 'dot-off'" aria-hidden="true" />{{ pendingCount ? 'To do' : 'Nothing pending' }}</span>
            <span>Approve access requests{{ pendingCount ? ` (${pendingCount} waiting)` : '' }}.</span>
            <button v-if="pendingCount" type="button" class="btn-secondary btn-sm" @click="setTab('users')">Review</button>
          </li>
          <li class="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-pico-border py-1.5">
            <span class="st tone-off w-28"><span class="dot dot-off" aria-hidden="true" />Optional</span>
            <span>Check cluster capacity on the <RouterLink to="/admin/health" class="link">Health</RouterLink> page.</span>
          </li>
        </ol>
      </section>

      <!-- eslint-disable-next-line vuejs-accessibility/interactive-supports-focus -- roving tabindex: the tab buttons inside are the focusable elements -->
      <div role="tablist" aria-label="Admin sections" class="flex gap-x-1 overflow-x-auto border-b border-pico-border" @keydown="onTabKey">
        <button
          v-for="t in [{ id: 'users', label: 'Users', n: users.length }, { id: 'apps', label: 'Apps', n: allApps.length }, { id: 'projects', label: 'Projects', n: null }, { id: 'suspensions', label: 'Suspensions', n: suspensionCount }, { id: 'servers', label: 'Database servers', n: servers.length }, { id: 'settings', label: 'Settings', n: null }, { id: 'audit', label: 'Audit', n: null }] as const"
          :id="`${uid}-tab-${t.id}`"
          :key="t.id"
          type="button"
          role="tab"
          :aria-selected="tab === t.id"
          :aria-controls="`${uid}-panel`"
          :tabindex="tab === t.id ? 0 : -1"
          class="-mb-px inline-flex min-h-[32px] items-center gap-1.5 whitespace-nowrap border-b-2 px-3 text-sm max-md:min-h-[44px]"
          :class="tab === t.id ? 'border-white font-medium text-white' : 'border-transparent text-zinc-400 hover:text-white'"
          @click="setTab(t.id)"
        >
          {{ t.label }}
          <span v-if="t.n !== null" class="meta mono">{{ t.n }}</span>
          <span v-if="t.id === 'users' && pendingCount" class="st tone-warn text-xs"><span class="dot dot-warn" aria-hidden="true" />{{ pendingCount }} pending</span>
        </button>
      </div>

      <div :id="`${uid}-panel`" role="tabpanel" :aria-labelledby="`${uid}-tab-${tab}`" class="space-y-3">
        <div v-if="tab !== 'servers' && tab !== 'settings' && tab !== 'projects' && tab !== 'suspensions'">
          <label :for="`${uid}-q`" class="sr-only">Search</label>
          <input :id="`${uid}-q`" v-model="q" type="search" class="input sm:max-w-xs" :placeholder="tab === 'users' ? 'Search users' : tab === 'apps' ? 'Search apps, owners, images' : 'Filter by action, actor, target'" />
        </div>

        <!-- USERS -->
        <template v-if="tab === 'users'">
          <section v-if="pendingUsers.length" aria-label="Waiting for approval" class="sec pt-2">
            <h2 class="section-title text-amber-300">Waiting for approval ({{ pendingUsers.length }})</h2>
            <div class="overflow-x-auto">
              <table class="tbl min-w-[40rem]">
                <colgroup><col /><col class="w-56" /><col class="w-20" /><col class="w-40" /></colgroup>
                <thead><tr><th scope="col">User</th><th scope="col">Requested</th><th scope="col" class="text-right">App limit</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
                <tbody>
                  <tr v-for="u in pendingUsers" :key="u.id">
                    <td class="truncate font-medium text-white" :title="u.email">{{ u.email }}</td>
                    <td class="meta" :title="fullDate(u.created_at)">{{ humanDate(u.created_at) }}</td>
                    <td class="mono text-right">{{ u.app_limit_effective }}</td>
                    <td>
                      <div class="row-acts">
                        <button type="button" class="row-act" :aria-label="approving === u.id ? 'Approving…' : `Approve ${u.email}`" :disabled="approving === u.id" @click="approve(u)">Approve</button>
                        <button type="button" class="row-act row-act-danger" :aria-label="`Reject ${u.email}`" @click="askUser('reject', u)">Reject</button>
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          <div class="overflow-x-auto">
            <table class="tbl min-w-[60rem]">
              <colgroup><col /><col class="w-28" /><col class="w-20" /><col class="w-14" /><col class="w-32" /><col class="w-[26rem]" /></colgroup>
              <thead>
                <tr><th scope="col">User</th><th scope="col">Status</th><th scope="col" class="text-right">Apps</th><th scope="col" class="text-right">Keys</th><th scope="col">App limit</th><th scope="col" class="text-right">Actions</th></tr>
              </thead>
              <tbody class="text-zinc-200">
                <tr v-for="u in otherUsers" :key="u.id">
                  <td class="truncate" :title="u.email">
                    <span class="font-medium text-white">{{ u.email }}</span>
                    <span v-if="u.role === 'platform_admin'" class="meta ml-1.5">Admin</span>
                    <span class="meta ml-1.5" :title="fullDate(u.created_at)">joined {{ humanDate(u.created_at) }}</span>
                  </td>
                  <td><StatusBadge :status="u.status" /></td>
                  <td class="mono whitespace-nowrap text-right">{{ u.app_count }}<span class="text-zinc-400"> / {{ u.app_limit_effective }}</span></td>
                  <td class="mono text-right">{{ u.api_key_count ?? 0 }}</td>
                  <td>
                    <span v-if="u.role === 'platform_admin'" class="mono">{{ u.app_limit_effective }}</span>
                    <form v-else class="flex items-center gap-1 whitespace-nowrap" @submit.prevent="saveLimit(u)">
                      <input v-model.number="limits[u.id]" type="text" inputmode="numeric" pattern="[0-9]*" :max="maxApps" :aria-label="`App limit for ${u.email}`" class="input mono w-14" />
                      <button v-if="limits[u.id] !== u.app_limit_effective" type="submit" class="row-act" :aria-label="`Save app limit for ${u.email}`" title="Save limit">Save</button>
                      <span v-else-if="saved[u.id]" class="st tone-ok text-xs" role="status">Saved</span>
                    </form>
                  </td>
                  <td>
                    <div class="row-acts">
                      <button type="button" class="row-act" :aria-label="`API keys of ${u.email}`" @click="keysUser = u">API keys</button>
                      <button v-if="u.role === 'platform_admin'" type="button" class="row-act" :aria-label="demoteBlocked(u) ? 'Remove admin (you are the only admin)' : `Remove admin from ${u.email}`" :disabled="demoteBlocked(u)" @click="askAdmin(u, false)">Remove admin</button>
                      <button v-else-if="u.status === 'active'" type="button" class="row-act" :aria-label="`Make ${u.email} an admin`" @click="askAdmin(u, true)">Make admin</button>
                      <template v-if="u.role !== 'platform_admin'">
                        <button v-if="u.status === 'suspended'" type="button" class="row-act" :aria-label="`Unsuspend ${u.email}`" @click="act(() => unsuspendUser(u.id), `${u.email} unsuspended.`, 'Couldn\'t unsuspend the user')">Unsuspend</button>
                        <button v-if="u.status === 'active'" type="button" class="row-act" :aria-label="`Suspend ${u.email}`" @click="askUser('suspend', u)">Suspend</button>
                        <button type="button" class="row-act" :aria-label="`Reset password for ${u.email}`" @click="askUser('reset', u)">Reset password</button>
                        <button type="button" class="row-act row-act-danger" :aria-label="`Delete ${u.email}`" @click="askUser('delete', u)">Delete</button>
                      </template>
                    </div>
                  </td>
                </tr>
                <tr v-if="loaded && otherUsers.length === 0"><td colspan="6" class="py-3 text-center text-zinc-400">{{ q ? 'No users match your search.' : 'No other users yet.' }}</td></tr>
                <tr v-else-if="!loaded"><td colspan="6" class="py-3 text-zinc-400" aria-busy="true">Loading users…</td></tr>
              </tbody>
            </table>
          </div>
        </template>

        <!-- APPS -->
        <div v-else-if="tab === 'apps'" class="overflow-x-auto">
          <table class="tbl min-w-[60rem]">
            <colgroup><col /><col class="w-52" /><col class="w-28" /><col class="w-64" /><col class="w-64" /></colgroup>
            <thead>
              <tr><th scope="col">App</th><th scope="col">Owner</th><th scope="col">Status</th><th scope="col">Resources</th><th scope="col" class="text-right">Actions</th></tr>
            </thead>
            <tbody class="text-zinc-200">
              <tr v-for="a in filteredApps" :key="a.id" class="align-top">
                <td class="truncate" :title="a.hostname">
                  <div class="mono truncate font-medium text-white" :title="a.name">{{ a.name }}</div>
                  <div class="mono truncate text-zinc-400">{{ a.hostname }}</div>
                </td>
                <td class="mono truncate text-zinc-300" :title="a.owner_email">{{ a.owner_email }}</td>
                <td><StatusBadge :status="a.status" /></td>
                <td>
                  <template v-if="a.resources">
                    <div class="mono truncate" :title="resSummary(a)">{{ resSummary(a) }}</div>
                    <div class="meta">
                      <span v-if="a.resources.overridden" title="Overridden: an admin set custom limits for this app.">overridden</span>
                      <span v-else>default</span>
                      <span v-if="a.resources.exceeds_platform_caps" class="st tone-warn ml-2" title="Exceeds caps: above the default per-app caps."><span class="dot dot-warn" aria-hidden="true" />exceeds caps</span>
                    </div>
                    <div v-if="overrideSummary(a)" class="mono wrap-break-word text-zinc-300" :title="overrideSummary(a)">{{ overrideSummary(a) }}</div>
                  </template>
                  <span v-else class="text-zinc-400">-</span>
                </td>
                <td>
                  <div class="row-acts">
                    <a :href="appUrl(a)" target="_blank" rel="noopener noreferrer" class="row-act" :aria-label="`Open ${a.name}`">Open</a>
                    <button type="button" class="row-act" :aria-label="`Edit limits for ${a.name}`" @click="openLimits(a)">Limits</button>
                    <button type="button" class="row-act" :aria-label="`Backups for ${a.name}`" @click="bkApp = a">Backups</button>
                    <button v-if="a.status === 'suspended' && a.suspended_via !== 'project' && a.suspended_via !== 'user'" type="button" class="row-act" :aria-label="`Unsuspend ${a.name}`" @click="act(() => unsuspendApp(a.id), `${a.name} unsuspended.`, 'Couldn\'t unsuspend the app')">Unsuspend</button>
                    <button type="button" class="row-act row-act-danger" :aria-label="`Delete ${a.name}`" @click="askDeleteApp(a)">Delete</button>
                  </div>
                </td>
              </tr>
              <tr v-if="loaded && filteredApps.length === 0"><td colspan="5" class="py-3 text-center text-zinc-400">{{ q ? 'No apps match your search.' : 'No apps yet.' }}</td></tr>
              <tr v-else-if="!loaded"><td colspan="5" class="py-3 text-zinc-400" aria-busy="true">Loading apps…</td></tr>
            </tbody>
          </table>
        </div>

        <!-- DB SERVERS -->
        <template v-else-if="tab === 'servers'">
          <div class="flex items-center justify-between gap-2">
            <p class="text-sm text-zinc-300">Database servers (MySQL or PostgreSQL) that app databases are placed on.</p>
            <IconButton label="Add database server" text="Add server" class="shrink-0" @click="openAdd" />
          </div>
          <div class="overflow-x-auto">
            <table class="tbl min-w-[48rem]">
              <colgroup><col class="w-32" /><col /><col class="w-28" /><col class="w-36" /><col class="w-36" /><col class="w-28" /></colgroup>
              <thead>
                <tr><th scope="col">Name</th><th scope="col">Host</th><th scope="col">Engine</th><th scope="col">Capacity</th><th scope="col">Status</th><th scope="col"><span class="sr-only">Actions</span></th></tr>
              </thead>
              <tbody class="text-zinc-200">
                <tr v-for="s in servers" :key="s.id">
                  <td class="truncate font-medium text-white" :title="s.name">{{ s.name }}</td>
                  <td class="mono truncate text-zinc-300" :title="`${s.host}:${s.port}`">{{ s.host }}:{{ s.port }}</td>
                  <td>{{ dbEngineLabel(s.engine) }}</td>
                  <td><DbCapacityBar :server="s" /></td>
                  <td>
                    <select
                      :value="s.status"
                      :aria-label="`Status of ${s.name}`"
                      :title="SERVER_STATUS_HELP[s.status] ?? ''"
                      class="input w-full"
                      @change="patch(s, { status: ($event.target as HTMLSelectElement).value })"
                    >
                      <option value="active">Active</option>
                      <option value="draining">Draining</option>
                      <option v-if="s.status === 'full'" value="full">Full (legacy)</option>
                    </select>
                  </td>
                  <td><div class="row-acts"><button type="button" class="row-act" :aria-label="`Change admin password of ${s.name}`" @click="openPw(s)">Password</button></div></td>
                </tr>
                <tr v-if="loaded && servers.length === 0">
                  <td colspan="6" class="py-4 text-center">
                    <p class="text-sm text-zinc-300">No database servers yet. Users can't create apps with a database until you add one.</p>
                    <button type="button" class="btn-primary mt-3" @click="openAdd">Add server</button>
                  </td>
                </tr>
                <tr v-else-if="!loaded"><td colspan="6" class="py-3 text-zinc-400" aria-busy="true">Loading database servers…</td></tr>
              </tbody>
            </table>
          </div>
          <p v-if="servers.length" class="help mt-2 max-w-3xl">
            <strong class="text-zinc-200">Status</strong> controls where <em>new</em> app databases go. <em>Active</em>
            servers take them (the one with the most free slots wins; a server at its max is skipped automatically).
            <em>Draining</em> takes none; databases already on it keep working and are never moved. Drain a server
            before retiring it. If no server can take a new database, users can't tick Database on new apps.
          </p>
        </template>

        <!-- SETTINGS -->
        <AdminProjects v-else-if="tab === 'projects'" @changed="refreshSuspensions" />
        <AdminSuspensions v-else-if="tab === 'suspensions'" :items="suspensions" :loaded="loaded" @changed="load" />
        <SettingsPanel v-else-if="tab === 'settings'" />

        <!-- AUDIT -->
        <template v-else>
        <div class="flex items-center justify-end gap-3 text-xs text-zinc-300">
          <span>Download full audit log</span>
          <IconButton label="Download audit log as CSV" text="CSV" @click="exportAudit('csv')" />
          <IconButton label="Download audit log as JSON" text="JSON" @click="exportAudit('json')" />
        </div>
        <div class="max-h-136 overflow-auto">
          <table class="tbl min-w-[56rem]">
            <colgroup><col class="w-40" /><col class="w-52" /><col class="w-44" /><col class="w-36" /><col /></colgroup>
            <thead>
              <tr class="sticky top-0 bg-pico-bg"><th scope="col">When</th><th scope="col">Actor</th><th scope="col">Action</th><th scope="col">Target</th><th scope="col">Detail</th></tr>
            </thead>
            <tbody class="text-zinc-200">
              <tr v-for="a in filteredAudit" :key="a.id" class="align-top">
                <td class="mono whitespace-nowrap text-zinc-300" :title="stampTitle(a.at)">{{ stamp(a.at) }}</td>
                <td class="mono truncate" :title="a.actor_email ?? undefined">{{ a.actor_email ?? '-' }}</td>
                <td class="mono truncate" :title="a.action">{{ a.action }}</td>
                <td class="mono truncate" :title="`${a.target_type ?? ''} ${a.target_id ?? ''}`">{{ a.target_type }} {{ a.target_id }}</td>
                <td class="wrap-break-word text-xs leading-4 text-zinc-300">{{ a.detail }}</td>
              </tr>
              <tr v-if="loaded && filteredAudit.length === 0"><td colspan="5" class="py-3 text-center text-zinc-400">No entries.</td></tr>
              <tr v-else-if="!loaded"><td colspan="5" class="py-3 text-zinc-400" aria-busy="true">Loading audit log…</td></tr>
            </tbody>
          </table>
        </div>
        </template>
      </div>
    </main>

    <!-- Change database server admin password -->
    <dialog
      ref="pwSrvDlg"
      :aria-labelledby="`${uid}-pws-title`"
      class="m-auto w-[calc(100%-2rem)] max-w-md rounded-md border border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/70"
      @cancel="pwSrvModal.onNativeCancel"
      @click="pwSrvModal.onBackdrop"
    >
      <form v-if="pwServer" class="space-y-3 p-4" @submit.prevent="savePw">
        <h2 :id="`${uid}-pws-title`" class="section-title">Change admin password: {{ pwServer.name }}</h2>
        <p class="help">Change the password on the database server first. The platform connects with the new one and only saves it if that works.</p>
        <PasswordField v-model="pwNew" label="New admin password" autocomplete="new-password" />
        <PasswordField v-model="pwConfirm" label="Confirm new password" autocomplete="new-password" :invalid="pwConfirm !== '' && pwConfirm !== pwNew" />
        <p v-if="pwConfirm !== '' && pwConfirm !== pwNew" class="text-sm text-red-300" role="alert">The passwords don't match.</p>
        <div class="flex justify-end gap-2">
          <button type="button" class="btn-secondary" :disabled="pwSaving" @click="pwServer = null">Cancel</button>
          <button type="submit" class="btn-primary" :disabled="pwSaving || !pwNew || pwNew !== pwConfirm">{{ pwSaving ? 'Verifying...' : 'Verify and save' }}</button>
        </div>
      </form>
    </dialog>

    <!-- Add database server -->
    <dialog
      ref="addDlg"
      :aria-labelledby="`${uid}-add-title`"
      class="m-auto max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-lg overflow-y-auto rounded-md border border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/70"
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
          <label :for="`${uid}-e`" class="label">Engine</label>
          <select :id="`${uid}-e`" v-model="form.engine" class="input">
            <option value="mysql">MySQL</option>
            <option value="pg">PostgreSQL</option>
          </select>
          <p class="help">Which database engine this server runs. Apps pick a matching engine when they add a database.</p>
        </div>
        <div>
          <label :for="`${uid}-h`" class="label">Host</label>
          <input :id="`${uid}-h`" v-model="form.host" required placeholder="private-xxxx.db.ondigitalocean.com" class="input mono" />
          <p class="help">Hostname the platform connects to. For a DO managed database use the private host.</p>
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
      class="m-auto w-[calc(100%-2rem)] max-w-md rounded-md border border-pico-border bg-pico-panel p-0 text-white shadow-xl backdrop:bg-black/70"
      @cancel="pwModal.onNativeCancel"
      @click="pwModal.onBackdrop"
    >
      <div v-if="tempPw" class="p-5">
        <h2 :id="`${uid}-pw-title`" class="section-title">Temporary password</h2>
        <p class="mt-2 text-sm text-zinc-300">
          Give this to <strong class="break-all text-white">{{ tempPw.email }}</strong>. It is shown once and can't be retrieved later. They should change it on the Account page.
        </p>
        <div class="mt-3 flex items-stretch gap-2">
          <code class="min-w-0 flex-1 select-all overflow-x-auto whitespace-nowrap rounded-sm bg-pico-card px-3 py-2 text-sm text-accent-light">{{ tempPw.password }}</code>
          <IconButton label="Copy password" text="Copy" class="shrink-0" data-autofocus @click="copyTemp" />
        </div>
        <div class="mt-5 flex justify-end"><button type="button" class="btn-secondary" @click="tempPw = null">Done</button></div>
      </div>
    </dialog>

    <SuspendDialog
      :open="suspendTarget !== null"
      :title="`Suspend ${suspendTarget?.email ?? ''}`"
      message="They can't sign in. Their personal project and its apps stop, and any Kaisen run they started stops. Their team projects keep running for the other members. Data is kept; unsuspend to restore."
      confirm-label="Suspend user"
      hint="Saved in the audit log and shown to admins in Suspensions."
      placeholder="Why?"
      :busy="susBusy"
      :error="susError"
      @confirm="confirmSuspendUser"
      @cancel="suspendTarget = null"
    />
    <UserKeysDialog :user="keysUser" @close="keysUser = null" @changed="refreshUsers" />
    <BackupAdminDialog :app="bkApp" @close="bkApp = null" />
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
</template>
