<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { useProjectsStore } from '../stores/projects'
import ApiKeysCard from '../components/ApiKeysCard.vue'
import PasswordField from '../components/PasswordField.vue'
import { changePassword, setTheme, setTimezone } from '../api/auth'
import { applyTheme, type Theme } from '../utils/theme'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'
import { useOnboarding } from '../composables/useOnboarding'
import { fullDate } from '../utils/format'
import { effectiveZone, listZones } from '../utils/tz'

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

const themeBusy = ref(false)
const themes: { id: Theme; label: string }[] = [
  { id: 'dark', label: 'Dark' },
  { id: 'light', label: 'Light' },
]
const theme = computed<Theme>(() => (auth.user?.theme === 'light' ? 'light' : 'dark'))
// Applied immediately (optimistic), saved to the profile; on failure the previous theme comes back with a toast.
async function onTheme(next: Theme) {
  const prev = theme.value
  if (next === prev || themeBusy.value) return
  applyTheme(next)
  themeBusy.value = true
  try {
    const u = await setTheme(next)
    if (auth.user) auth.user.theme = u?.theme ?? next
  } catch (err) {
    applyTheme(prev)
    toast.error(err instanceof ApiError ? err.message : "Couldn't save the theme.")
  } finally {
    themeBusy.value = false
  }
}
// Arrow keys move between the two options (radio-group behaviour); each move selects.
function onThemeKey(e: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return
  e.preventDefault()
  void onTheme(theme.value === 'dark' ? 'light' : 'dark')
  void nextTick(() => (document.querySelector('[data-theme-opt][aria-checked="true"]') as HTMLElement | null)?.focus())
}

const auth = useAuthStore()
const onboarding = useOnboarding()
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
  <main id="main" tabindex="-1" class="page-main space-y-3">
    <h1 class="page-title">Account</h1>

    <div class="grid gap-x-8 gap-y-3 lg:grid-cols-2 lg:items-start">
      <section class="sec pt-3" aria-label="Profile">
        <dl class="grid grid-cols-[7rem_1fr] gap-y-2 text-sm">
          <dt class="text-zinc-400">Email</dt>
          <dd class="mono truncate text-white" :title="auth.user?.email">{{ auth.user?.email }}</dd>
          <dt class="text-zinc-400">Role</dt>
          <dd class="text-white">{{ auth.user?.role === 'platform_admin' ? 'Platform admin' : 'User' }}</dd>
          <dt class="text-zinc-400">App limit</dt>
          <dd class="mono text-white">{{ auth.user?.app_limit_effective ?? auth.user?.app_limit }}</dd>
          <dt class="text-zinc-400">Joined</dt>
          <dd class="mono text-white">{{ auth.user ? fullDate(auth.user.created_at) : '' }}</dd>
          <dt class="text-zinc-400">Getting started</dt>
          <dd><button type="button" class="row-act" @click="onboarding.show()">Show getting started</button></dd>
          <dt id="theme-label" class="text-zinc-400">Theme</dt>
          <dd class="min-w-0">
            <div role="radiogroup" aria-labelledby="theme-label" class="inline-flex rounded-md border border-zinc-500 p-0.5">
              <button
                v-for="t in themes"
                :key="t.id"
                type="button"
                role="radio"
                data-theme-opt
                :aria-checked="theme === t.id"
                :tabindex="theme === t.id ? 0 : -1"
                :disabled="themeBusy"
                :class="['min-h-[28px] rounded-sm px-3 text-sm transition-colors disabled:cursor-wait max-md:min-h-[36px]', theme === t.id ? 'bg-pico-muted font-medium text-white' : 'text-zinc-300 hover:bg-pico-card hover:text-white']"
                @click="onTheme(t.id)"
                @keydown="onThemeKey"
              >{{ t.label }}</button>
            </div>
            <p class="mt-1 text-xs text-zinc-400">Saved to your profile; used on every device.</p>
          </dd>
          <dt class="text-zinc-400"><label for="tz">Time zone</label></dt>
          <dd class="min-w-0 space-y-1">
            <input v-model="zoneFilter" type="search" class="input" placeholder="Filter zones…" aria-label="Filter time zones" />
            <select id="tz" class="input w-full min-w-0 truncate" :value="auth.user?.timezone ?? ''" :disabled="zoneBusy" @change="onZone">
              <option value="">Browser default</option>
              <option v-for="z in shownZones" :key="z" :value="z">{{ z }}</option>
            </select>
            <p class="text-xs text-zinc-500">Times shown in <span class="mono">{{ effectiveZone() }}</span></p>
            <p v-if="zoneErr" class="field-error" role="alert">{{ zoneErr }}</p>
          </dd>
        </dl>
      </section>

      <section class="sec pt-3" aria-labelledby="pw-title">
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

    <section class="sec pt-3" aria-labelledby="proj-title">
      <h2 id="proj-title" class="section-title">Projects</h2>
      <p class="help">Share apps with other people in a project. Your personal project stays private to you.</p>
      <p v-if="projects.loadError" role="alert" class="field-error mt-2">{{ projects.loadError }}</p>
      <template v-if="projects.projects.length">
        <table class="tbl mt-2 max-[759px]:hidden">
          <colgroup>
            <col />
            <col class="w-24" />
            <col class="w-24" />
            <col class="w-24" />
            <col class="w-44" />
          </colgroup>
          <thead>
            <tr>
              <th scope="col">Project</th>
              <th scope="col">Role</th>
              <th scope="col">Apps</th>
              <th scope="col">Members</th>
              <th scope="col" class="text-right"><span class="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in projects.projects" :key="p.id">
              <td class="truncate"><RouterLink :to="`/projects/${p.id}`" class="font-medium text-white hover:underline">{{ p.is_personal ? 'Personal project' : p.name }}</RouterLink></td>
              <td class="text-zinc-300">{{ p.is_personal ? 'Personal' : p.role === 'owner' ? 'Owner' : 'Member' }}</td>
              <td class="mono">{{ p.app_count }}</td>
              <td class="mono">{{ p.is_personal ? '' : p.member_count }}</td>
              <td class="text-right">
                <div class="row-acts">
                  <RouterLink v-if="!p.is_personal && p.role === 'owner'" class="row-act max-[759px]:!min-h-[44px]" :aria-label="`Invite people to ${p.name}`" :to="`/projects/${p.id}#invite`">Invite</RouterLink>
                  <RouterLink v-if="!p.is_personal" class="row-act max-[759px]:!min-h-[44px]" :aria-label="p.role === 'owner' ? `Manage ${p.name}` : `View ${p.name}`" :to="`/projects/${p.id}`">{{ p.role === 'owner' ? 'Manage' : 'View' }}</RouterLink>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <ul class="mt-2 divide-y divide-pico-border border-y border-pico-border min-[760px]:hidden">
          <li v-for="p in projects.projects" :key="p.id" class="flex items-center justify-between gap-2 py-1">
            <RouterLink :to="`/projects/${p.id}`" class="min-w-0 flex-1 py-1">
              <div class="truncate text-sm font-medium text-white">{{ p.is_personal ? 'Personal project' : p.name }}</div>
              <div class="meta">{{ p.is_personal ? 'personal' : p.role }} · {{ p.app_count }} {{ p.app_count === 1 ? 'app' : 'apps' }}<template v-if="!p.is_personal"> · {{ p.member_count }} {{ p.member_count === 1 ? 'member' : 'members' }}</template></div>
            </RouterLink>
            <div class="row-acts shrink-0 gap-x-3">
              <RouterLink v-if="!p.is_personal && p.role === 'owner'" class="row-act max-[759px]:!min-h-[44px]" :aria-label="`Invite people to ${p.name}`" :to="`/projects/${p.id}#invite`">Invite</RouterLink>
              <RouterLink v-if="!p.is_personal" class="row-act max-[759px]:!min-h-[44px]" :aria-label="p.role === 'owner' ? `Manage ${p.name}` : `View ${p.name}`" :to="`/projects/${p.id}`">{{ p.role === 'owner' ? 'Manage' : 'View' }}</RouterLink>
            </div>
          </li>
        </ul>
      </template>
      <p v-if="projects.loaded && !projects.projects.some((p) => !p.is_personal)" class="meta mt-2">No shared projects yet. Create one from the <RouterLink to="/projects" class="link">Projects page</RouterLink>, then invite people from it.</p>
    </section>

    <ApiKeysCard />
  </main>
</template>
