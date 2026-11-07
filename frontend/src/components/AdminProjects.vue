<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import IconButton from './IconButton.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import SuspendDialog from './SuspendDialog.vue'
import { getKaisenUploadsSummary, listAdminProjectsPage, setProjectAppLimit, setProjectFeatures, suspendProject, unsuspendProject } from '../api/admin'
import { ApiError } from '../api/client'
import { useToastStore } from '../stores/toast'
import ProjectFeaturesMenu from './ProjectFeaturesMenu.vue'
import { fmtSize } from '../composables/kaisen'
import type { AdminProject, KaisenUploadsPlatformSummary } from '../types'
import { fullDate, humanDate } from '../utils/format'

const emit = defineEmits<{ changed: [] }>()

// Admin > Projects: shared projects and their app-limit override (blank = platform default).
const toast = useToastStore()
const rows = ref<AdminProject[]>([])
const loading = ref(true)
const error = ref('')
const rowError = reactive<Record<number, string>>({})
const draft = reactive<Record<number, string>>({})

const PAGE = 25
const q = ref('')
const offset = ref(0)
const total = ref(0)
let seq = 0
let timer: ReturnType<typeof setTimeout> | null = null
const orig = (p: AdminProject) => (p.app_limit === null ? '' : String(p.app_limit))
// Rows without an override show the platform default as their effective limit.
const defaultLimit = computed(() => rows.value.find((p) => p.app_limit === null)?.app_limit_effective ?? null)
const from = computed(() => (total.value ? offset.value + 1 : 0))
const to = computed(() => Math.min(offset.value + PAGE, total.value))

async function load() {
  const mine = ++seq
  loading.value = true
  error.value = ''
  try {
    const page = await listAdminProjectsPage(q.value.trim(), PAGE, offset.value)
    if (mine !== seq) return
    rows.value = page.items
    total.value = page.total
    for (const p of rows.value) draft[p.id] = orig(p)
  } catch (e) {
    if (mine === seq) error.value = e instanceof ApiError ? e.message : "Couldn't load projects."
  } finally {
    if (mine === seq) loading.value = false
  }
}
function onSearch() {
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => {
    offset.value = 0
    load()
  }, 250)
}
function page(delta: number) {
  offset.value = Math.max(0, offset.value + delta * PAGE)
  load()
}
onBeforeUnmount(() => timer && clearTimeout(timer))
onMounted(load)

const summary = ref<KaisenUploadsPlatformSummary | null>(null)
getKaisenUploadsSummary().then((s) => (summary.value = s)).catch(() => (summary.value = null))
const flagBusy = ref<number | null>(null)
// Optimistic: flip the row, roll back and show a toast if the server refuses.
async function toggleFeature(p: AdminProject, field: 'kaisen_web_search' | 'kaisen_uploads', value: boolean) {
  const key = field === 'kaisen_web_search' ? 'kaisen_web_search_enabled' : 'kaisen_uploads_enabled'
  const before = p[key]
  p[key] = value
  flagBusy.value = p.id
  try {
    await setProjectFeatures(p.id, { [field]: value })
  } catch (e) {
    p[key] = before
    toast.error(`Couldn't change ${field === 'kaisen_web_search' ? 'web search' : 'file uploads'} for ${p.name}: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    flagBusy.value = null
  }
}

async function save(p: AdminProject) {
  const raw = (draft[p.id] ?? '').trim()
  const value = raw === '' ? null : Number(raw)
  if (value !== null && (!Number.isInteger(value) || value < 0)) {
    rowError[p.id] = 'Enter a whole number, or leave blank for the default.'
    return
  }
  rowError[p.id] = ''
  try {
    const u = await setProjectAppLimit(p.id, value)
    rows.value = rows.value.map((x) => (x.id === p.id ? u : x))
    draft[p.id] = orig(u)
    toast.success(`Updated the limit for ${p.name}.`)
  } catch (e) {
    rowError[p.id] = e instanceof ApiError ? e.message : "Couldn't save the limit."
  }
}

// ---- suspend / unsuspend (reason required to suspend; fully reversible, so no type-the-name) ----
const susTarget = ref<AdminProject | null>(null)
const unsTarget = ref<AdminProject | null>(null)
const susBusy = ref(false)
const susError = ref('')
const appsWord = (n: number) => `${n} ${n === 1 ? 'app' : 'apps'}`
function openSuspend(p: AdminProject) {
  susError.value = ''
  susTarget.value = p
}
async function confirmSuspend(reason: string) {
  const p = susTarget.value
  if (!p) return
  susBusy.value = true
  susError.value = ''
  try {
    const u = await suspendProject(p.id, reason)
    rows.value = rows.value.map((x) => (x.id === p.id ? u : x))
    susTarget.value = null
    toast.success(`${p.name} suspended.`)
    emit('changed')
  } catch (e) {
    susError.value = e instanceof ApiError ? e.message : "Couldn't suspend the project."
  } finally {
    susBusy.value = false
  }
}
async function confirmUnsuspend() {
  const p = unsTarget.value
  if (!p) return
  susBusy.value = true
  try {
    const u = await unsuspendProject(p.id)
    rows.value = rows.value.map((x) => (x.id === p.id ? u : x))
    unsTarget.value = null
    toast.success(`${p.name} unsuspended.`)
    emit('changed')
  } catch (e) {
    unsTarget.value = null
    rowError[p.id] = e instanceof ApiError ? e.message : "Couldn't unsuspend the project."
  } finally {
    susBusy.value = false
  }
}
</script>

<template>
  <div>
    <div class="mb-3">
      <h2 class="section-title">Projects</h2>
      <p v-if="summary" class="help">Kaisen upload storage: {{ fmtSize(summary.platform_bytes) }} of {{ fmtSize(summary.platform_limit_bytes) }} ({{ Math.round(summary.percent) }}%{{ summary.warn ? ', nearly full' : '' }}), {{ summary.files }} files.</p>
      <p class="help">App limit is the most apps a project may hold. Leave it blank to use the default from settings<template v-if="defaultLimit !== null"> ({{ defaultLimit }} apps)</template>. Features turn Kaisen web search and file uploads on or off per project.</p>
    </div>
    <div class="mb-3">
      <label for="ap-q" class="label">Search projects</label>
      <input id="ap-q" v-model="q" type="search" class="input sm:max-w-sm" autocomplete="off" placeholder="Project name or owner email" @input="onSearch" />
    </div>

    <div v-if="error" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
      <span>{{ error }}</span>
      <IconButton label="Retry" text="Retry" @click="load" />
    </div>
    <p v-else-if="loading && rows.length === 0" class="meta" aria-busy="true">Loading projects…</p>
    <p v-else-if="rows.length === 0" class="sec meta py-2">{{ q.trim() ? 'No projects match that search.' : 'No projects yet.' }}</p>
    <template v-else>
    <div class="overflow-x-auto" :aria-busy="loading">
      <table class="tbl min-w-[60rem]">
        <colgroup><col /><col class="w-20" /><col class="w-24" /><col class="w-52" /><col class="w-36" /><col class="w-40" /><col class="w-44" /></colgroup>
        <thead>
          <tr><th scope="col">Project</th><th scope="col" class="text-right">Apps</th><th scope="col" class="text-right">Members</th><th scope="col">Owner</th><th scope="col">App limit</th><th scope="col">Features</th><th scope="col"><span class="sr-only">Action</span></th></tr>
        </thead>
        <tbody class="text-zinc-300">
          <template v-for="p in rows" :key="p.id">
            <tr>
              <td class="truncate" :title="p.name">
                <span class="font-medium text-white">{{ p.name }}</span>
                <span v-if="p.is_personal" class="meta ml-1.5">just you</span>
                <span v-if="p.status === 'suspended'" class="st tone-off ml-1.5 text-xs"><span class="dot dot-off" aria-hidden="true" />Suspended</span>
              </td>
              <td class="mono text-right">{{ p.app_count }} of {{ p.app_limit_effective }}<span class="sr-only"> apps</span></td>
              <td class="mono text-right">{{ p.member_count }}<span class="sr-only"> {{ p.member_count === 1 ? 'member' : 'members' }}</span></td>
              <td class="truncate" :title="p.owner_emails.join(', ')">{{ p.owner_emails.join(', ') || 'none' }}</td>
              <td>
                <form class="flex items-center gap-1" @submit.prevent="save(p)">
                  <label :for="`pl-${p.id}`" class="sr-only">App limit</label>
                  <input :id="`pl-${p.id}`" v-model="draft[p.id]" type="text" inputmode="numeric" pattern="[0-9]*" :placeholder="String(p.app_limit_effective)" class="input mono w-16" />
                  <button v-if="draft[p.id] !== orig(p)" type="submit" class="row-act" :aria-label="`Save app limit for ${p.name}`" title="Save limit">Save</button>
                </form>
              </td>
              <td><ProjectFeaturesMenu :project="p" :busy="flagBusy === p.id" @toggle="(f, v) => toggleFeature(p, f, v)" /></td>
              <td>
                <div class="row-acts">
                  <template v-if="!p.is_personal">
                    <IconButton v-if="p.status === 'suspended'" :label="`Unsuspend ${p.name}`" verb="Unsuspend" sm @click="unsTarget = p" />
                    <IconButton v-else :label="`Suspend ${p.name}`" verb="Suspend" sm danger @click="openSuspend(p)" />
                  </template>
                </div>
              </td>
            </tr>
            <tr v-if="p.status === 'suspended' || rowError[p.id]">
              <td colspan="7">
                <p v-if="p.status === 'suspended'" class="meta wrap-break-word text-zinc-300">
                  <span :title="p.suspended_at ? fullDate(p.suspended_at) : undefined">{{ p.suspended_at ? humanDate(p.suspended_at) : 'Suspended' }}</span>: {{ p.suspended_reason || 'No reason recorded.' }}
                </p>
                <p v-if="rowError[p.id]" role="alert" class="field-error">{{ rowError[p.id] }}</p>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
    <nav class="mt-3 flex flex-wrap items-center justify-between gap-2" aria-label="Projects pages">
      <span class="meta" role="status">{{ from }}–{{ to }} of {{ total }}</span>
      <span class="flex gap-2">
        <button type="button" class="btn-secondary btn-sm" :disabled="offset === 0 || loading" @click="page(-1)">Previous</button>
        <button type="button" class="btn-secondary btn-sm" :disabled="offset + PAGE >= total || loading" @click="page(1)">Next</button>
      </span>
    </nav>
    </template>

    <SuspendDialog
      :open="susTarget !== null"
      :title="`Suspend ${susTarget?.name ?? ''}`"
      :message="`${appsWord(susTarget?.app_count ?? 0)} will stop and show a 503 page. Kaisen builds in this project stop too, and no new ones can start. Members can still view the project and download backups. Nothing is deleted.`"
      confirm-label="Suspend project"
      hint="Saved in the audit log and shown to the project's owners."
      placeholder="What happened? Project owners see this."
      :busy="susBusy"
      :error="susError"
      @confirm="confirmSuspend"
      @cancel="susTarget = null"
    />
    <ConfirmDialog
      :open="unsTarget !== null"
      :title="`Unsuspend ${unsTarget?.name ?? ''}`"
      :message="`${appsWord(unsTarget?.app_count ?? 0)} start again. Apps suspended on their own stay suspended. Kaisen builds that were stopped do not resume; members can start new ones.`"
      confirm-label="Unsuspend project"
      busy-label="Unsuspending…"
      :busy="susBusy"
      @confirm="confirmUnsuspend"
      @cancel="unsTarget = null"
    />
  </div>
</template>
