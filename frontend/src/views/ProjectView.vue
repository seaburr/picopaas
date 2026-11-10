<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import IconButton from '../components/IconButton.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import CopyButton from '../components/CopyButton.vue'
import SuspendedBanner from '../components/SuspendedBanner.vue'
import AppNotFound from '../components/AppNotFound.vue'
import { ApiError } from '../api/client'
import * as api from '../api/projects'
import { useAuthStore } from '../stores/auth'
import { useProjectsStore } from '../stores/projects'
import { useToastStore } from '../stores/toast'
import type { Project, ProjectInvite, ProjectInviteCreated, ProjectMember, ProjectRole } from '../types'
import { fullDate, humanDate } from '../utils/format'
import { SUSPENDED_TITLE } from '../composables/projectSuspension'

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
const susp = computed(() => project.value?.status === 'suspended')
const suspTitle = computed(() => (susp.value ? SUSPENDED_TITLE : undefined))

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
    await router.push('/projects')
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
    await router.push('/projects')
  } catch (e) {
    deleteOpen.value = false
    memberError.value = err(e, "Couldn't delete the project.")
  } finally {
    actionBusy.value = false
  }
}
</script>

<template>
  <div>
    <main id="main" tabindex="-1" class="page-main space-y-4">
      <nav class="meta" aria-label="Breadcrumb">
        <RouterLink to="/projects" class="link">Projects</RouterLink><span aria-hidden="true"> / </span>{{ pid === null ? 'New project' : project ? (project.is_personal ? 'Personal project' : project.name) : 'Project' }}
      </nav>

      <!-- create -->
      <template v-if="pid === null">
        <h1 class="page-title">New project</h1>
        <form class="sec max-w-3xl space-y-3 pt-4" @submit.prevent="onCreate">
          <div>
            <label for="p-name" class="label">Name</label>
            <input id="p-name" v-model="newName" class="input" maxlength="80" required :aria-invalid="!!createError" autocomplete="off" />
            <p class="help">A project shares its apps with the people you invite. Your personal project stays as it is.</p>
          </div>
          <p v-if="createError" role="alert" class="field-error">{{ createError }}</p>
          <div class="flex gap-2">
            <button type="submit" class="btn-primary" :disabled="creating || !newName.trim()" :aria-busy="creating">{{ creating ? 'Creating…' : 'Create project' }}</button>
            <RouterLink :to="store.appsHome" class="btn-secondary">Cancel</RouterLink>
          </div>
        </form>
      </template>

      <!-- detail -->
      <template v-else>
        <div v-if="loading" class="sec pt-4" aria-busy="true" aria-label="Loading project">
          <div class="h-4 w-40 animate-pulse rounded-sm bg-pico-card" />
        </div>
        <AppNotFound v-else-if="gone" />
        <div v-else-if="loadError" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
          <span>{{ loadError }}</span>
          <IconButton text="Retry" label="Retry" @click="load" />
        </div>
        <template v-else-if="project">
          <SuspendedBanner :project="project" />
          <div class="flex flex-wrap items-baseline justify-between gap-2">
            <h1 class="page-title break-all">{{ project.is_personal ? 'Personal project' : project.name }}</h1>
            <span class="meta"><span class="mono">{{ project.app_count }} of {{ project.app_limit_effective }}</span> apps · {{ project.member_count }} {{ project.member_count === 1 ? 'member' : 'members' }}</span>
          </div>
          <p v-if="project.is_personal" class="help">
            Your personal project can't be shared, renamed or deleted. To work with others, <RouterLink to="/projects/new" class="underline">create a new project</RouterLink> and invite them to it.
          </p>
          <p v-if="memberError" role="alert" class="alert-error">{{ memberError }}</p>

          <form v-if="shared && isOwner" class="sec max-w-3xl pt-4" @submit.prevent="onRename">
            <label for="p-rename" class="label">Name</label>
            <div class="flex gap-2">
              <input id="p-rename" v-model="nameDraft" class="input" maxlength="80" required autocomplete="off" :disabled="susp" :aria-invalid="!!renameError" />
              <IconButton text="Rename" :label="susp ? 'Rename project (project is suspended)' : 'Rename project'" :busy="renaming" :disabled="susp" @click="onRename" />
            </div>
            <p v-if="renameError" role="alert" class="field-error mt-1">{{ renameError }}</p>
          </form>

          <section v-if="shared" class="sec pt-4" aria-labelledby="h-members">
            <h2 id="h-members" class="section-title">Members</h2>
            <p v-if="members.length === 0" class="meta mt-1">No members.</p>
            <template v-else>
              <table class="tbl mt-1 max-[759px]:hidden">
                <colgroup>
                  <col />
                  <col class="w-40" />
                  <col class="w-32" />
                  <col class="w-28" />
                </colgroup>
                <thead>
                  <tr>
                    <th scope="col">Member</th>
                    <th scope="col">Joined</th>
                    <th scope="col">Role</th>
                    <th scope="col" class="text-right"><span class="sr-only">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="m in members" :key="m.user_id">
                    <td class="truncate" :title="m.email"><span class="text-white">{{ m.email }}</span><span v-if="isMe(m)" class="meta ml-1">(you)</span></td>
                    <td class="meta" :title="fullDate(m.added_at)">{{ humanDate(m.added_at) }}</td>
                    <td>
                      <select v-if="isOwner" class="input w-auto" :aria-label="`Role of ${m.email}`" :value="m.role" :disabled="busyUser === m.user_id || susp" :title="suspTitle" @change="onRole(m, $event)">
                        <option value="owner">Owner</option>
                        <option value="member">Member</option>
                      </select>
                      <span v-else class="text-zinc-300">{{ m.role === 'owner' ? 'Owner' : 'Member' }}</span>
                    </td>
                    <td class="text-right">
                      <div class="row-acts">
                        <button v-if="isMe(m)" type="button" class="row-act row-act-danger max-[759px]:!min-h-[44px]" :aria-label="susp ? 'Leave project (project is suspended)' : 'Leave project'" :title="susp ? SUSPENDED_TITLE : undefined" :disabled="susp" @click="leaving = true">Leave</button>
                        <button v-else-if="isOwner" type="button" class="row-act row-act-danger max-[759px]:!min-h-[44px]" :aria-label="susp ? `Remove ${m.email} (project is suspended)` : `Remove ${m.email}`" :disabled="susp" @click="removing = m">Remove</button>
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>

              <ul class="mt-1 divide-y divide-pico-border border-y border-pico-border min-[760px]:hidden">
                <li v-for="m in members" :key="m.user_id" class="py-2">
                  <div class="break-all text-sm text-white">{{ m.email }}<span v-if="isMe(m)" class="meta ml-1">(you)</span></div>
                  <div class="meta" :title="fullDate(m.added_at)">Joined {{ humanDate(m.added_at) }}</div>
                  <div class="mt-1 flex flex-wrap items-center gap-x-4">
                    <select v-if="isOwner" class="input w-auto" :aria-label="`Role of ${m.email}`" :value="m.role" :disabled="busyUser === m.user_id || susp" :title="suspTitle" @change="onRole(m, $event)">
                      <option value="owner">Owner</option>
                      <option value="member">Member</option>
                    </select>
                    <span v-else class="text-sm text-zinc-300">{{ m.role === 'owner' ? 'Owner' : 'Member' }}</span>
                    <button v-if="isMe(m)" type="button" class="row-act row-act-danger max-[759px]:!min-h-[44px]" :aria-label="susp ? 'Leave project (project is suspended)' : 'Leave project'" :title="susp ? SUSPENDED_TITLE : undefined" :disabled="susp" @click="leaving = true">Leave</button>
                    <button v-else-if="isOwner" type="button" class="row-act row-act-danger max-[759px]:!min-h-[44px]" :aria-label="susp ? `Remove ${m.email} (project is suspended)` : `Remove ${m.email}`" :disabled="susp" @click="removing = m">Remove</button>
                  </div>
                </li>
              </ul>
            </template>
          </section>

          <section v-if="shared && isOwner" id="invite" class="sec scroll-mt-4 pt-4" aria-labelledby="h-invite">
            <h2 id="h-invite" class="section-title">Invite people to this project</h2>
            <p class="help max-w-3xl">No email is sent. Create an invite and share the link. It works once and expires in 7 days. Leave the email blank to let anyone with an approved account use it.</p>
            <form class="mt-2 flex max-w-3xl flex-col gap-2 sm:flex-row sm:items-end" @submit.prevent="onInvite">
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
              <button type="submit" class="btn-secondary" :disabled="inviting || susp" :title="suspTitle" :aria-busy="inviting">{{ inviting ? 'Creating…' : 'Create invite' }}</button>
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
            <template v-if="invites.length">
              <table class="tbl mt-1 max-[759px]:hidden">
                <colgroup>
                  <col />
                  <col class="w-28" />
                  <col class="w-40" />
                  <col class="w-28" />
                </colgroup>
                <thead>
                  <tr>
                    <th scope="col">Invited</th>
                    <th scope="col">Role</th>
                    <th scope="col">Expires</th>
                    <th scope="col" class="text-right"><span class="sr-only">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="i in invites" :key="i.id">
                    <td class="truncate text-white">{{ i.email ?? 'Anyone with the link' }}</td>
                    <td class="text-zinc-300">{{ i.role }}</td>
                    <td class="meta" :title="fullDate(i.expires_at)">{{ humanDate(i.expires_at) }}</td>
                    <td class="text-right">
                      <div class="row-acts">
                        <button type="button" class="row-act row-act-danger max-[759px]:!min-h-[44px]" :aria-label="susp ? 'Revoke invite (project is suspended)' : 'Revoke invite'" :disabled="susp" @click="onRevoke(i)">Revoke</button>
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
              <ul class="mt-1 divide-y divide-pico-border border-y border-pico-border min-[760px]:hidden">
                <li v-for="i in invites" :key="i.id" class="flex items-center justify-between gap-2 py-1">
                  <div class="min-w-0 text-sm">
                    <div class="break-all text-white">{{ i.email ?? 'Anyone with the link' }}</div>
                    <div class="meta">{{ i.role }} · expires {{ humanDate(i.expires_at) }}</div>
                  </div>
                  <button type="button" class="row-act row-act-danger max-[759px]:!min-h-[44px]" :aria-label="susp ? 'Revoke invite (project is suspended)' : 'Revoke invite'" :disabled="susp" @click="onRevoke(i)">Revoke</button>
                </li>
              </ul>
            </template>
            <p v-else class="meta mt-1">No pending invites.</p>
          </section>

          <section v-if="shared && isOwner" class="sec-danger" aria-labelledby="h-pdanger">
            <h2 id="h-pdanger" class="section-title">Danger zone</h2>
            <div class="mt-2 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <p class="text-sm text-zinc-300">Delete this project. It must have no apps; move or delete them first.</p>
              <button type="button" class="btn-danger shrink-0" :disabled="project.app_count > 0 || susp" :title="suspTitle" @click="deleteOpen = true">
                Delete project…
              </button>
            </div>
          </section>
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
