<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { LogOut, Pencil, Plus, RotateCw, Trash2, X } from 'lucide-vue-next'
import NavBar from '../components/NavBar.vue'
import IconButton from '../components/IconButton.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import CopyButton from '../components/CopyButton.vue'
import AppNotFound from '../components/AppNotFound.vue'
import { ApiError } from '../api/client'
import * as api from '../api/projects'
import { useAuthStore } from '../stores/auth'
import { useProjectsStore } from '../stores/projects'
import { useToastStore } from '../stores/toast'
import type { Project, ProjectInvite, ProjectInviteCreated, ProjectMember, ProjectRole } from '../types'
import { fullDate, humanDate } from '../utils/format'

// /projects/new (no id) = create form; /projects/:id = members, invites, rename, delete.
const props = defineProps<{ id?: string }>()
const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const store = useProjectsStore()
const toast = useToastStore()
const err = (e: unknown, fallback: string) => (e instanceof ApiError ? e.message : fallback)

// ---- create ----
const newName = ref('')
const creating = ref(false)
const createError = ref('')
async function onCreate() {
  creating.value = true
  createError.value = ''
  try {
    const p = await store.create(newName.value.trim())
    toast.success(`Created ${p.name}.`)
    await router.push(`/projects/${p.id}`)
  } catch (e) {
    createError.value = err(e, "Couldn't create the project.")
  } finally {
    creating.value = false
  }
}

// ---- detail ----
const pid = computed(() => (props.id !== undefined ? Number(props.id) : null))
const project = ref<Project | null>(null)
const members = ref<ProjectMember[]>([])
const invites = ref<ProjectInvite[]>([])
const loading = ref(true)
const loadError = ref('')
const gone = ref(false)
const isOwner = computed(() => project.value?.role === 'owner')
const shared = computed(() => !!project.value && !project.value.is_personal)

async function load() {
  if (pid.value === null) return
  loading.value = true
  loadError.value = ''
  gone.value = false
  try {
    project.value = await api.getProject(pid.value)
    members.value = (await api.listMembers(pid.value)) ?? []
    invites.value = project.value.role === 'owner' && !project.value.is_personal ? ((await api.listInvites(pid.value)) ?? []) : []
    store.upsert(project.value)
    nameDraft.value = project.value.name
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) gone.value = true
    else loadError.value = err(e, "Couldn't load the project.")
  } finally {
    loading.value = false
    if (route.hash === '#invite') {
      await nextTick()
      document.getElementById('invite')?.scrollIntoView({ block: 'start' })
    }
  }
}
onMounted(load)
watch(pid, load)

// rename
const nameDraft = ref('')
const renaming = ref(false)
const renameError = ref('')
async function onRename() {
  if (!project.value) return
  renaming.value = true
  renameError.value = ''
  try {
    project.value = await api.renameProject(project.value.id, nameDraft.value.trim())
    store.upsert(project.value)
    toast.success('Renamed.')
  } catch (e) {
    renameError.value = err(e, "Couldn't rename the project.")
  } finally {
    renaming.value = false
  }
}

// members
const memberError = ref('')
const busyUser = ref<number | null>(null)
const isMe = (m: ProjectMember) => m.user_id === auth.user?.id
async function onRole(m: ProjectMember, e: Event) {
  const sel = e.target as HTMLSelectElement
  const role = sel.value as ProjectRole
  busyUser.value = m.user_id
  memberError.value = ''
  try {
    const u = await api.setMemberRole(project.value!.id, m.user_id, role)
    members.value = members.value.map((x) => (x.user_id === m.user_id ? u : x))
    await load()
  } catch (er) {
    sel.value = m.role
    memberError.value = err(er, "Couldn't change the role.")
  } finally {
    busyUser.value = null
  }
}
const removing = ref<ProjectMember | null>(null)
const leaving = ref(false)
const actionBusy = ref(false)
async function confirmRemove() {
  const m = removing.value
  if (!m || !project.value) return
  actionBusy.value = true
  memberError.value = ''
  try {
    await api.removeMember(project.value.id, m.user_id)
    toast.success(`Removed ${m.email}.`)
    removing.value = null
    await load()
  } catch (e) {
    removing.value = null
    memberError.value = err(e, "Couldn't remove the member.")
  } finally {
    actionBusy.value = false
  }
}
async function confirmLeave() {
  if (!project.value) return
  actionBusy.value = true
  try {
    const id = project.value.id
    await api.leaveProject(id)
    store.drop(id)
    toast.success('You left the project.')
    await router.push('/apps')
  } catch (e) {
    leaving.value = false
    memberError.value = err(e, "Couldn't leave the project.")
  } finally {
    actionBusy.value = false
  }
}

// invites
const inviteEmail = ref('')
const inviteRole = ref<ProjectRole>('member')
const inviting = ref(false)
const inviteError = ref('')
const created = ref<ProjectInviteCreated | null>(null)
const inviteUrl = computed(() => (created.value ? `${window.location.origin}${created.value.path}` : ''))
async function onInvite() {
  if (!project.value) return
  inviting.value = true
  inviteError.value = ''
  try {
    const email = inviteEmail.value.trim()
    created.value = await api.createInvite(project.value.id, { ...(email ? { email } : {}), role: email ? inviteRole.value : 'member' })
    inviteEmail.value = ''
    invites.value = (await api.listInvites(project.value.id)) ?? []
  } catch (e) {
    inviteError.value = err(e, "Couldn't create the invite.")
  } finally {
    inviting.value = false
  }
}
async function onRevoke(i: ProjectInvite) {
  if (!project.value) return
  try {
    await api.revokeInvite(project.value.id, i.id)
    invites.value = invites.value.filter((x) => x.id !== i.id)
    toast.success('Invite revoked.')
  } catch (e) {
    inviteError.value = err(e, "Couldn't revoke the invite.")
  }
}

// delete
const deleteOpen = ref(false)
async function confirmDelete() {
  if (!project.value) return
  actionBusy.value = true
  try {
    const id = project.value.id
    await api.deleteProject(id)
    store.drop(id)
    toast.success('Project deleted.')
    await router.push('/apps')
  } catch (e) {
    deleteOpen.value = false
    memberError.value = err(e, "Couldn't delete the project.")
  } finally {
    actionBusy.value = false
  }
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main id="main" tabindex="-1" class="page-main space-y-3">
      <!-- create -->
      <template v-if="pid === null">
        <h1 class="page-title">New project</h1>
        <form class="card card-pad space-y-3 [&_.input]:max-w-3xl" @submit.prevent="onCreate">
          <div>
            <label for="p-name" class="label">Name</label>
            <input id="p-name" v-model="newName" class="input" maxlength="80" required :aria-invalid="!!createError" autocomplete="off" />
            <p class="help">A project shares its apps with the people you invite. Your personal project stays as it is.</p>
          </div>
          <p v-if="createError" role="alert" class="field-error">{{ createError }}</p>
          <div class="flex gap-2">
            <button type="submit" class="btn-primary" :disabled="creating || !newName.trim()" :aria-busy="creating">{{ creating ? 'Creating…' : 'Create project' }}</button>
            <RouterLink to="/apps" class="btn-secondary">Cancel</RouterLink>
          </div>
        </form>
      </template>

      <!-- detail -->
      <template v-else>
        <div v-if="loading" class="card card-pad" aria-busy="true" aria-label="Loading project">
          <div class="h-4 w-40 animate-pulse rounded-sm bg-pico-card" />
        </div>
        <AppNotFound v-else-if="gone" />
        <div v-else-if="loadError" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
          <span>{{ loadError }}</span>
          <IconButton :icon="RotateCw" label="Retry" @click="load" />
        </div>
        <template v-else-if="project">
          <div class="flex flex-wrap items-baseline justify-between gap-2">
            <h1 class="page-title break-all">{{ project.is_personal ? 'Personal project' : project.name }}</h1>
            <span class="meta">{{ project.app_count }} of {{ project.app_limit_effective }} apps · {{ project.member_count }} {{ project.member_count === 1 ? 'member' : 'members' }}</span>
          </div>
          <p v-if="project.is_personal" class="help">
            Your personal project can't be shared, renamed or deleted. To work with others, <RouterLink to="/projects/new" class="underline">create a new project</RouterLink> and invite them to it.
          </p>
          <p v-if="memberError" role="alert" class="alert-error">{{ memberError }}</p>

          <form v-if="shared && isOwner" class="card card-pad" @submit.prevent="onRename">
            <label for="p-rename" class="label">Name</label>
            <div class="flex gap-2">
              <input id="p-rename" v-model="nameDraft" class="input" maxlength="80" required autocomplete="off" :aria-invalid="!!renameError" />
              <IconButton :icon="Pencil" label="Rename project" :busy="renaming" @click="onRename" />
            </div>
            <p v-if="renameError" role="alert" class="field-error mt-1">{{ renameError }}</p>
          </form>

          <section v-if="shared && isOwner" id="invite" class="card card-pad" aria-labelledby="h-invite">
            <h2 id="h-invite" class="section-title">Invite people to this project</h2>
            <p class="help">No email is sent. Create an invite and share the link. It works once and expires in 7 days. Leave the email blank to let anyone with an approved account use it.</p>
            <form class="mt-2 flex flex-col gap-2 sm:flex-row sm:items-end" @submit.prevent="onInvite">
              <div class="min-w-0 flex-1">
                <label for="inv-email" class="label">Email (optional)</label>
                <input id="inv-email" v-model="inviteEmail" type="email" class="input" autocomplete="off" />
              </div>
              <div>
                <label for="inv-role" class="label">Role</label>
                <select id="inv-role" v-model="inviteRole" class="input">
                  <option value="member">Member</option>
                  <option v-if="inviteEmail.trim()" value="owner">Owner (email required)</option>
                </select>
              </div>
              <button type="submit" class="btn-secondary" :disabled="inviting" :aria-busy="inviting"><Plus :size="16" aria-hidden="true" /> {{ inviting ? 'Creating…' : 'Create invite' }}</button>
            </form>
            <p v-if="inviteError" role="alert" class="field-error mt-2">{{ inviteError }}</p>
            <div v-if="created" class="alert-info mt-3" role="status">
              <p class="text-sm">Invite link (shown once, copy it now):</p>
              <div class="mt-1 flex items-center gap-2">
                <code class="mono min-w-0 flex-1 break-all text-xs">{{ inviteUrl }}</code>
                <CopyButton :text="inviteUrl" label="Copy invite link" />
              </div>
            </div>

            <h3 class="mt-4 text-sm font-medium text-white">Pending invites</h3>
            <ul v-if="invites.length" class="mt-1 divide-y divide-pico-border">
              <li v-for="i in invites" :key="i.id" class="flex items-center justify-between gap-2 py-1.5">
                <div class="min-w-0 text-sm">
                  <span class="truncate text-white">{{ i.email ?? 'Anyone with the link' }}</span>
                  <span class="meta ml-2">{{ i.role }} · expires {{ humanDate(i.expires_at) }}</span>
                </div>
                <IconButton :icon="X" label="Revoke invite" danger sm @click="onRevoke(i)" />
              </li>
            </ul>
            <p v-else class="meta mt-1">No pending invites.</p>
          </section>

          <section v-if="shared" class="card" aria-labelledby="h-members">
            <h2 id="h-members" class="section-title card-pad pb-0">Members</h2>
            <ul class="divide-y divide-pico-border">
              <li v-for="m in members" :key="m.user_id" class="flex flex-wrap items-center justify-between gap-2 px-3 py-2 sm:px-4">
                <div class="min-w-0">
                  <div class="truncate text-sm text-white" :title="m.email">{{ m.email }}<span v-if="isMe(m)" class="meta ml-1">(you)</span></div>
                  <div class="meta" :title="fullDate(m.added_at)">Joined {{ humanDate(m.added_at) }}</div>
                </div>
                <div class="flex shrink-0 items-center gap-1">
                  <select v-if="isOwner" class="input w-auto" :aria-label="`Role of ${m.email}`" :value="m.role" :disabled="busyUser === m.user_id" @change="onRole(m, $event)">
                    <option value="owner">Owner</option>
                    <option value="member">Member</option>
                  </select>
                  <span v-else class="badge-info">{{ m.role === 'owner' ? 'Owner' : 'Member' }}</span>
                  <IconButton v-if="isMe(m)" :icon="LogOut" label="Leave project" danger @click="leaving = true" />
                  <IconButton v-else-if="isOwner" :icon="X" :label="`Remove ${m.email}`" danger @click="removing = m" />
                </div>
              </li>
            </ul>
            <p v-if="members.length === 0" class="meta px-4 py-3">No members.</p>
          </section>

          <section v-if="shared && isOwner" class="card card-pad border-danger/40" aria-labelledby="h-pdanger">
            <h2 id="h-pdanger" class="section-title text-danger-light">Danger zone</h2>
            <div class="mt-2 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <p class="text-sm text-zinc-300">Delete this project. It must have no apps; move or delete them first.</p>
              <button type="button" class="btn-danger shrink-0" :disabled="project.app_count > 0" @click="deleteOpen = true">
                <Trash2 :size="16" aria-hidden="true" /> Delete project…
              </button>
            </div>
          </section>
          <RouterLink to="/apps" class="btn-secondary inline-flex">Back to apps</RouterLink>
        </template>
      </template>
    </main>

    <ConfirmDialog
      :open="deleteOpen"
      title="Delete project"
      :message="`This permanently deletes ${project?.name ?? ''}, its members and pending invites. This can't be undone.`"
      confirm-label="Delete project"
      busy-label="Deleting…"
      danger
      :busy="actionBusy"
      :require-text="project?.name"
      @confirm="confirmDelete"
      @cancel="deleteOpen = false"
    />
    <ConfirmDialog
      :open="removing !== null"
      title="Remove member"
      :message="`${removing?.email ?? ''} loses access to this project's apps right away, including their API keys.`"
      confirm-label="Remove"
      busy-label="Removing…"
      danger
      :busy="actionBusy"
      @confirm="confirmRemove"
      @cancel="removing = null"
    />
    <ConfirmDialog
      :open="leaving"
      title="Leave project"
      :message="`You lose access to ${project?.name ?? 'this project'} and its apps. An owner has to invite you again to rejoin.`"
      confirm-label="Leave"
      busy-label="Leaving…"
      danger
      :busy="actionBusy"
      @confirm="confirmLeave"
      @cancel="leaving = false"
    />
  </div>
</template>
