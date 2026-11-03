<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Ban, Check, Play, RotateCw } from 'lucide-vue-next'
import IconButton from './IconButton.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import SuspendDialog from './SuspendDialog.vue'
import { getKaisenUploadsFlag, getKaisenUploadsSummary, listAdminProjects, setKaisenUploadsFlag, setProjectAppLimit, suspendProject, unsuspendProject } from '../api/admin'
import { ApiError } from '../api/client'
import { useToastStore } from '../stores/toast'
import { fmtSize } from '../composables/kaisen'
import type { AdminProject, KaisenUploadsFlag, KaisenUploadsPlatformSummary } from '../types'
import { fullDate, humanDate } from '../utils/format'

const emit = defineEmits<{ changed: [] }>()

// Admin > Projects: shared projects and their app-limit override (blank = platform default).
const toast = useToastStore()
const rows = ref<AdminProject[]>([])
const loading = ref(true)
const error = ref('')
const rowError = reactive<Record<number, string>>({})
const draft = reactive<Record<number, string>>({})

// Rows without an override show the platform default as their effective limit.
const defaultLimit = computed(() => rows.value.find((p) => p.app_limit === null)?.app_limit_effective ?? null)
const orig = (p: AdminProject) => (p.app_limit === null ? '' : String(p.app_limit))

async function load() {
  loading.value = true
  error.value = ''
  try {
    rows.value = (await listAdminProjects()) ?? []
    for (const p of rows.value) draft[p.id] = orig(p)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load projects."
  } finally {
    loading.value = false
  }
}
onMounted(load)

// ---- Kaisen uploads: per-project switch (default off). Personal projects are not in the list, so an id lookup too. ----
const flagError = ref('')
const flagBusy = ref<number | null>(null)
const lookupId = ref('')
const lookup = ref<KaisenUploadsFlag | null>(null)
const summary = ref<KaisenUploadsPlatformSummary | null>(null)
getKaisenUploadsSummary().then((s) => (summary.value = s)).catch(() => (summary.value = null))

async function toggleRow(p: AdminProject, enabled: boolean) {
  flagError.value = ''
  flagBusy.value = p.id
  try {
    const r = await setKaisenUploadsFlag(p.id, enabled)
    rows.value = rows.value.map((x) => (x.id === p.id ? { ...x, kaisen_uploads_enabled: r.kaisen_uploads_enabled } : x))
    toast.success(`Kaisen uploads ${r.kaisen_uploads_enabled ? 'on' : 'off'} for ${r.name}.`)
  } catch (e) {
    flagError.value = e instanceof ApiError ? e.message : "Couldn't change Kaisen uploads."
    rows.value = rows.value.map((x) => ({ ...x }))
  } finally {
    flagBusy.value = null
  }
}
async function findProject() {
  flagError.value = ''
  lookup.value = null
  const id = Number(lookupId.value.trim())
  if (!Number.isInteger(id) || id < 1) {
    flagError.value = 'Enter a project id (a whole number).'
    return
  }
  try {
    lookup.value = await getKaisenUploadsFlag(id)
  } catch (e) {
    flagError.value = e instanceof ApiError ? e.message : "Couldn't look up that project."
  }
}
async function toggleLookup(enabled: boolean) {
  if (!lookup.value) return
  flagError.value = ''
  try {
    lookup.value = await setKaisenUploadsFlag(lookup.value.project_id, enabled)
    toast.success(`Kaisen uploads ${lookup.value.kaisen_uploads_enabled ? 'on' : 'off'} for ${lookup.value.name}.`)
  } catch (e) {
    flagError.value = e instanceof ApiError ? e.message : "Couldn't change Kaisen uploads."
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
    <section class="card card-pad mb-3 space-y-2" aria-labelledby="ku-h">
      <h2 id="ku-h" class="section-title">Kaisen file uploads</h2>
      <p class="help">Off for every project until you turn it on. Find any project by id, including personal ones, or use the switch on a row below.<template v-if="summary"> Platform storage: {{ fmtSize(summary.platform_bytes) }} of {{ fmtSize(summary.platform_limit_bytes) }} ({{ Math.round(summary.percent) }}%{{ summary.warn ? ', nearly full' : '' }}), {{ summary.files }} files.</template></p>
      <form class="flex flex-wrap items-end gap-2" novalidate @submit.prevent="findProject">
        <div>
          <label for="ku-id" class="label">Project id</label>
          <input id="ku-id" v-model="lookupId" type="text" inputmode="numeric" pattern="[0-9]*" class="input mono w-32" />
        </div>
        <button type="submit" class="btn-secondary">Look up</button>
        <label v-if="lookup" class="flex items-center gap-2 text-sm">
          <input type="checkbox" :checked="lookup.kaisen_uploads_enabled" @change="toggleLookup(($event.target as HTMLInputElement).checked)" />
          Uploads on for <strong class="font-medium text-white">{{ lookup.name }}</strong> (#{{ lookup.project_id }})
        </label>
      </form>
      <p v-if="flagError" role="alert" class="field-error">{{ flagError }}</p>
    </section>
    <div v-if="error" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
      <span>{{ error }}</span>
      <IconButton :icon="RotateCw" label="Retry" @click="load" />
    </div>
    <p v-else-if="loading" class="meta" aria-busy="true">Loading projects…</p>
    <p v-else-if="rows.length === 0" class="card card-pad meta">No shared projects yet.</p>
    <template v-else>
    <p class="help mb-2">
      App limit is the most apps a project may hold. Leave it blank to use the default from settings ({{ defaultLimit ?? 'unknown' }} apps).
    </p>
    <ul class="card divide-y divide-pico-border">
      <li v-for="p in rows" :key="p.id" class="flex flex-col gap-2 px-3 py-2 sm:flex-row sm:items-center sm:justify-between sm:px-4">
        <div class="min-w-0">
          <div class="flex min-w-0 flex-wrap items-center gap-1.5">
            <span class="truncate font-medium text-white" :title="p.name">{{ p.name }}</span>
            <span v-if="p.status === 'suspended'" class="badge-error">Suspended</span>
          </div>
          <div class="meta break-all" :title="p.owner_emails.join(', ')">
            {{ p.app_count }} of {{ p.app_limit_effective }} apps · {{ p.member_count }} {{ p.member_count === 1 ? 'member' : 'members' }} · owner {{ p.owner_emails.join(', ') || 'none' }}
          </div>
          <p v-if="p.status === 'suspended'" class="meta wrap-break-word text-zinc-300">
            <span :title="p.suspended_at ? fullDate(p.suspended_at) : undefined">{{ p.suspended_at ? humanDate(p.suspended_at) : 'Suspended' }}</span>: {{ p.suspended_reason || 'No reason recorded.' }}
          </p>
          <p v-if="rowError[p.id]" role="alert" class="field-error">{{ rowError[p.id] }}</p>
        </div>
        <form class="flex shrink-0 flex-wrap items-center gap-1" @submit.prevent="save(p)">
          <label :for="`pl-${p.id}`" class="meta">App limit <span class="text-zinc-500">(effective {{ p.app_limit_effective }})</span></label>
          <input :id="`pl-${p.id}`" v-model="draft[p.id]" type="text" inputmode="numeric" pattern="[0-9]*" placeholder="blank = default" class="input mono w-40" />
          <button v-if="draft[p.id] !== orig(p)" type="submit" class="btn-icon btn-icon-sm text-accent-light" :aria-label="`Save app limit for ${p.name}`" title="Save limit">
            <Check :size="16" aria-hidden="true" />
          </button>
          <label class="meta flex items-center gap-1">
            <input type="checkbox" :checked="p.kaisen_uploads_enabled === true" :disabled="flagBusy === p.id" @change="toggleRow(p, ($event.target as HTMLInputElement).checked)" />
            Kaisen uploads<span class="sr-only"> for {{ p.name }}</span>
          </label>
          <IconButton v-if="p.status === 'suspended'" :icon="Play" :label="`Unsuspend ${p.name}`" sm @click="unsTarget = p" />
          <IconButton v-else :icon="Ban" :label="`Suspend ${p.name}`" sm danger @click="openSuspend(p)" />
        </form>
      </li>
    </ul>
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
