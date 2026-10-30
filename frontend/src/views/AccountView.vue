<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Settings2, UserPlus } from 'lucide-vue-next'
import IconButton from '../components/IconButton.vue'
import { useProjectsStore } from '../stores/projects'
import NavBar from '../components/NavBar.vue'
import ApiKeysCard from '../components/ApiKeysCard.vue'
import PasswordField from '../components/PasswordField.vue'
import { changePassword, setTimezone } from '../api/auth'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'
import { fullDate } from '../utils/format'
import { browserZone, effectiveZone, listZones } from '../utils/tz'

const zones = listZones()
const zoneFilter = ref('')
const zoneErr = ref('')
const zoneBusy = ref(false)
const shownZones = computed(() => {
  const q = zoneFilter.value.trim().toLowerCase()
  const cur = auth.user?.timezone
  const list = q ? zones.filter((z) => z.toLowerCase().includes(q)) : zones
  return cur && !list.includes(cur) ? [cur, ...list] : list
})
async function onZone(e: Event) {
  const v = (e.target as HTMLSelectElement).value
  zoneErr.value = ''
  zoneBusy.value = true
  try {
    const u = await setTimezone(v === '' ? null : v)
    if (auth.user) auth.user.timezone = u?.timezone ?? (v === '' ? null : v)
    toast.success('Time zone saved.')
  } catch (err) {
    zoneErr.value = err instanceof ApiError ? err.message : "Couldn't save the time zone."
    ;(e.target as HTMLSelectElement).value = auth.user?.timezone ?? ''
  } finally {
    zoneBusy.value = false
  }
}

const auth = useAuthStore()
const toast = useToastStore()
const projects = useProjectsStore()
onMounted(() => { void projects.load() })
const form = reactive({ current: '', next: '', confirm: '' })
const error = ref('')
const loading = ref(false)
const mismatch = computed(() => form.confirm !== '' && form.confirm !== form.next)

async function onSubmit() {
  error.value = ''
  if (form.next !== form.confirm) {
    error.value = "The new passwords don't match."
    return
  }
  loading.value = true
  try {
    await changePassword(form.current, form.next)
    form.current = form.next = form.confirm = ''
    if (auth.user) auth.user.must_change_password = false
    toast.success('Password changed.')
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) error.value = "Couldn't change your password here. Ask the administrator to reset it."
    else error.value = e instanceof ApiError ? e.message : "Couldn't change your password. Try again."
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main id="main" tabindex="-1" class="page-main space-y-3">
      <h1 class="page-title">Account</h1>

      <div class="grid gap-3 lg:grid-cols-2 lg:items-start">
        <section class="card card-pad" aria-label="Profile">
          <dl class="grid grid-cols-[7rem_1fr] gap-y-2 text-sm">
            <dt class="text-zinc-400">Email</dt>
            <dd class="truncate text-white" :title="auth.user?.email">{{ auth.user?.email }}</dd>
            <dt class="text-zinc-400">Role</dt>
            <dd class="text-white">{{ auth.user?.role === 'platform_admin' ? 'Platform admin' : 'User' }}</dd>
            <dt class="text-zinc-400">App limit</dt>
            <dd class="mono text-white">{{ auth.user?.app_limit_effective ?? auth.user?.app_limit }}</dd>
            <dt class="text-zinc-400">Joined</dt>
            <dd class="mono text-white">{{ auth.user ? fullDate(auth.user.created_at) : '' }}</dd>
            <dt class="text-zinc-400"><label for="tz">Time zone</label></dt>
            <dd class="space-y-1">
              <input v-model="zoneFilter" type="search" class="input" placeholder="Filter zones…" aria-label="Filter time zones" />
              <select id="tz" class="input" :value="auth.user?.timezone ?? ''" :disabled="zoneBusy" @change="onZone">
                <option value="">Browser default ({{ browserZone() }})</option>
                <option v-for="z in shownZones" :key="z" :value="z">{{ z }}</option>
              </select>
              <p class="text-xs text-zinc-500">Times shown in {{ effectiveZone() }}</p>
              <p v-if="zoneErr" class="field-error" role="alert">{{ zoneErr }}</p>
            </dd>
          </dl>
        </section>
  
        <section class="card card-pad" aria-labelledby="proj-title">
          <h2 id="proj-title" class="section-title">Projects</h2>
          <p class="help">Share apps with other people in a project. Your personal project stays private to you.</p>
          <p v-if="projects.loadError" role="alert" class="field-error mt-2">{{ projects.loadError }}</p>
          <ul v-if="projects.projects.length" class="mt-2 divide-y divide-pico-border rounded-sm border border-pico-border">
            <li v-for="p in projects.projects" :key="p.id" class="flex items-center justify-between gap-2 px-3 py-2">
              <RouterLink :to="`/projects/${p.id}`" class="min-w-0 flex-1">
                <div class="truncate text-sm font-medium text-white">
                  {{ p.is_personal ? 'Personal project' : p.name }}
                  <span v-if="p.is_personal" class="badge-info ml-1">personal</span>
                  <span v-else class="badge-info ml-1">{{ p.role }}</span>
                </div>
                <div class="meta">{{ p.app_count }} {{ p.app_count === 1 ? 'app' : 'apps' }}<template v-if="!p.is_personal"> · {{ p.member_count }} {{ p.member_count === 1 ? 'member' : 'members' }}</template></div>
              </RouterLink>
              <div class="flex shrink-0 items-center gap-1">
                <IconButton v-if="!p.is_personal && p.role === 'owner'" :icon="UserPlus" :label="`Invite people to ${p.name}`" :to="`/projects/${p.id}#invite`" />
                <IconButton v-if="!p.is_personal" :icon="Settings2" :label="p.role === 'owner' ? `Manage ${p.name}` : `View ${p.name}`" :to="`/projects/${p.id}`" />
              </div>
            </li>
          </ul>
          <p v-if="projects.loaded && !projects.projects.some((p) => !p.is_personal)" class="meta mt-2">No shared projects yet. Create one, then invite people from it.</p>
          <RouterLink to="/projects/new" class="btn-secondary mt-2 inline-flex">New project</RouterLink>
        </section>

        <section class="card card-pad" aria-labelledby="pw-title">
          <h2 id="pw-title" class="section-title">Change password</h2>
          <form class="mt-3 max-w-sm space-y-3" @submit.prevent="onSubmit">
            <PasswordField v-model="form.current" label="Current password" autocomplete="current-password" />
            <PasswordField v-model="form.next" label="New password" autocomplete="new-password" :minlength="8" help="At least 8 characters." />
            <div>
              <PasswordField v-model="form.confirm" label="Confirm new password" autocomplete="new-password" :minlength="8" :invalid="mismatch" />
              <p v-if="mismatch" class="field-error">Passwords don't match.</p>
            </div>
            <div v-if="error" class="alert-error" role="alert">{{ error }}</div>
            <button type="submit" class="btn-primary" :disabled="loading || mismatch">{{ loading ? 'Saving…' : 'Change password' }}</button>
          </form>
        </section>
      </div>

      <ApiKeysCard />
    </main>
  </div>
</template>
