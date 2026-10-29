<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import ConfirmDialog from './ConfirmDialog.vue'
import CopyButton from './CopyButton.vue'
import DeleteAppDialog from './DeleteAppDialog.vue'
import { moveApp } from '../api/projects'
import { useProjectsStore } from '../stores/projects'
import { clearCustomDomain, rotateCredentials, setCustomDomain, verifyCustomDomain } from '../api/apps'
import { clearPlatformHost, setPlatformHost } from '../api/admin'
import { ApiError } from '../api/client'
import { useAppsStore } from '../stores/apps'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'
import type { App } from '../types'

// Settings not in the YAML spec: custom domain, credentials, short address, delete.
const props = defineProps<{ app: App }>()
const emit = defineEmits<{ 'update:app': [app: App] }>()
const router = useRouter()
const store = useAppsStore()
const auth = useAuthStore()
const toast = useToastStore()
const projects = useProjectsStore()

// Move to another project: needs the owner role in both.
const fromProject = computed(() => projects.projects.find((p) => p.id === app.value.project_id) ?? null)
const moveTargets = computed(() => projects.projects.filter((p) => p.role === 'owner' && p.id !== app.value.project_id))
const canMove = computed(() => fromProject.value?.role === 'owner' && moveTargets.value.length > 0)
const moveTo = ref<number | null>(null)
const moving = ref(false)
const moveError = ref('')
async function onMove() {
  if (moveTo.value === null) return
  moving.value = true
  moveError.value = ''
  try {
    const a = await moveApp(app.value.id, moveTo.value)
    const target = projects.projects.find((p) => p.id === moveTo.value)
    moveTo.value = null
    setApp(a)
    projects.load()
    toast.success(`Moved to ${target?.is_personal ? 'Personal' : (target?.name ?? 'the project')}.`)
  } catch (e) {
    moveError.value = e instanceof ApiError ? e.message : "Couldn't move the app."
  } finally {
    moving.value = false
  }
}

// Local view of the app; actions replace it and notify the parent.
const app = ref<App>(props.app)
watch(() => props.app, (a) => { app.value = a })
function setApp(a: App) {
  app.value = a
  emit('update:app', a)
}
onBeforeUnmount(stopDomainPoll)

// Credentials
const rotating = ref<'db' | 'storage' | null>(null)
const rotateBusy = ref(false)
async function confirmRotate() {
  const target = rotating.value
  if (!target) return
  rotateBusy.value = true
  try {
    await rotateCredentials(app.value.id, target)
    toast.success(target === 'db' ? 'Database password rotated. Restarting the app to pick it up.' : 'Blob storage keys rotated. Restarting the app to pick them up.')
    rotating.value = null
    setApp(await store.fetchOne(app.value.id))
  } catch (e) {
    toast.error(`Couldn't rotate: ${e instanceof ApiError ? e.message : 'unknown error'}`)
    rotating.value = null
  } finally {
    rotateBusy.value = false
  }
}

// Reserved platform subdomain (admin only)
const isAdmin = computed(() => auth.user?.role === 'platform_admin')
const subInput = ref('')
const subBusy = ref(false)
const subError = ref('')
watch(() => app.value?.platform_subdomain, (v) => { subInput.value = v ?? '' }, { immediate: true })
function subMsg(e: unknown) {
  if (e instanceof ApiError && e.status === 409) return 'That subdomain is taken.'
  return e instanceof ApiError ? e.message : 'unknown error'
}
async function saveSub() {
  if (!app.value || !subInput.value.trim()) return
  subBusy.value = true
  subError.value = ''
  try {
    setApp(await setPlatformHost(app.value.id, subInput.value.trim().toLowerCase()))
    store.upsert(app.value)
    toast.success(`Now also served at ${app.value.platform_host}.`)
  } catch (e) {
    subError.value = subMsg(e)
  } finally {
    subBusy.value = false
  }
}
async function clearSub() {
  if (!app.value) return
  subBusy.value = true
  subError.value = ''
  try {
    setApp(await clearPlatformHost(app.value.id))
    store.upsert(app.value)
    toast.success('Short address removed.')
  } catch (e) {
    subError.value = subMsg(e)
  } finally {
    subBusy.value = false
  }
}

// Custom domain
const cdom = computed(() => app.value?.custom_domain ?? null)
const isApex = computed(() => (cdom.value?.host ?? '').split('.').filter(Boolean).length <= 2)
const newDomain = ref('')
const domainBusy = ref(false)
const verifyBusy = ref(false)
const clearingDomain = ref(false)
async function saveDomain() {
  if (!app.value || !newDomain.value.trim()) return
  domainBusy.value = true
  try {
    setApp(await setCustomDomain(app.value.id, newDomain.value.trim()))
    newDomain.value = ''
    toast.success('Domain added. Create the two DNS records below, then verify.')
  } catch (e) {
    toast.error(`Couldn't add the domain: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    domainBusy.value = false
  }
}
async function verifyDomain(auto = false) {
  if (!app.value) return
  if (!auto) verifyBusy.value = true
  try {
    setApp(await verifyCustomDomain(app.value.id))
    if (app.value.custom_domain?.status === 'active') {
      if (!auto) toast.success('Verified. Issuing the certificate and serving your domain.')
      else toast.success(`${app.value.custom_domain.host} is verified. Your certificate is on its way.`)
    } else if (!auto) toast.error('Not verified yet. Check the DNS records and try again.')
  } catch (e) {
    if (!auto) toast.error(`Verify failed: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    if (!auto) verifyBusy.value = false
  }
}

// Bounded auto-verify: every 20s, up to 15 attempts (~5 min), while the domain is pending. Silent on failure.
const DOMAIN_POLL_MS = 20000
const DOMAIN_POLL_MAX = 15
let domainTimer: ReturnType<typeof setInterval> | null = null
let domainTries = 0
const domainPolling = ref(false)
function stopDomainPoll() {
  if (domainTimer) clearInterval(domainTimer)
  domainTimer = null
  domainPolling.value = false
}
async function domainTick() {
  if (cdom.value?.status !== 'pending') return stopDomainPoll()
  // Hidden-tab and in-flight ticks don't count toward the budget; only real checks do.
  if (verifyBusy.value || document.hidden) return
  if (++domainTries > DOMAIN_POLL_MAX) return stopDomainPoll()
  await verifyDomain(true)
}
// The backend refuses domain verify on a suspended app (403), so don't poll then.
watch(
  () => [cdom.value?.host, cdom.value?.status, app.value?.status] as const,
  ([host, status, appStatus]) => {
    stopDomainPoll()
    if (host && status === 'pending' && appStatus !== 'suspended') {
      domainTries = 0
      domainPolling.value = true
      domainTimer = setInterval(domainTick, DOMAIN_POLL_MS)
    }
  },
  { immediate: true },
)

const removeDomainOpen = ref(false)
async function removeDomain() {
  if (!app.value) return
  clearingDomain.value = true
  stopDomainPoll()
  try {
    setApp(await clearCustomDomain(app.value.id))
    toast.success('Custom domain removed.')
  } catch (e) {
    toast.error(`Couldn't remove the domain: ${e instanceof ApiError ? e.message : 'unknown error'}`)
  } finally {
    clearingDomain.value = false
    removeDomainOpen.value = false
  }
}

// Delete
const deleteOpen = ref(false)
const deleting = ref(false)
async function confirmDelete() {
  if (!app.value) return
  deleting.value = true
  const name = app.value.name
  try {
    await store.remove(app.value.id)
    toast.success(`Deleted ${name}.`)
    await router.push('/apps')
  } catch (e) {
    toast.error(`Couldn't delete ${name}: ${e instanceof ApiError ? e.message : 'unknown error'}`)
    deleteOpen.value = false
  } finally {
    deleting.value = false
  }
}

const hasAddons = computed(() => app.value.want_db || app.value.want_storage)
const suspended = computed(() => app.value.status === 'suspended')
</script>

<template>
  <div>
        <div class="space-y-3">
          <section class="card card-pad" aria-labelledby="h-domain">
            <h2 id="h-domain" class="section-title mb-1">Custom domain</h2>
            <p class="help">
              Serve this app on your own domain in addition to <span class="mono">{{ app.hostname }}</span>. We issue a
              free TLS certificate automatically once DNS is verified.
            </p>

            <!-- None set -->
            <form v-if="!cdom" class="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center" @submit.prevent="saveDomain">
              <label for="domain-input" class="sr-only">Custom domain</label>
              <input id="domain-input" v-model="newDomain" placeholder="blog.example.com" class="input sm:max-w-xs" autocomplete="off" :disabled="suspended" />
              <button type="submit" class="btn-secondary" :disabled="domainBusy || suspended || !newDomain.trim()">{{ domainBusy ? 'Adding…' : 'Add domain' }}</button>
            </form>

            <!-- Set -->
            <div v-else class="mt-3 space-y-3">
              <div class="flex flex-wrap items-center gap-2">
                <span class="mono text-zinc-100">{{ cdom.host }}</span>
                <span class="badge-info" :class="cdom.status === 'active' ? 'bg-emerald-500/15 text-emerald-300' : 'bg-amber-500/15 text-amber-300'">
                  {{ cdom.status === 'active' ? 'Active' : 'Pending verification' }}
                </span>
                <a v-if="cdom.status === 'active'" :href="`https://${cdom.host}`" target="_blank" rel="noopener" class="link text-sm">Open ↗</a>
              </div>

              <div v-if="cdom.status !== 'active'" class="rounded-md border border-pico-border bg-pico-card p-3 text-sm">
                <p class="mb-2 text-zinc-300">Create these two DNS records at your domain provider, then verify:</p>
                <div class="overflow-x-auto">
                  <table class="w-full min-w-lg text-left text-xs">
                    <caption class="sr-only">DNS records to create</caption>
                    <thead class="meta">
                      <tr>
                        <th class="py-1 pr-3 font-medium">Record Type</th>
                        <th class="py-1 pr-3 font-medium">Host Name</th>
                        <th class="py-1 font-medium">Value</th>
                      </tr>
                    </thead>
                    <tbody class="divide-y divide-pico-border align-top">
                      <tr>
                        <td class="py-2 pr-3 mono text-zinc-200">CNAME</td>
                        <td class="py-2 pr-3"><div class="flex items-center gap-1"><span class="mono break-all text-zinc-200">{{ cdom.host }}</span><CopyButton :text="cdom.host" sm label="Copy CNAME host name" /></div></td>
                        <td class="py-2"><div class="flex items-center gap-1"><span class="mono break-all text-zinc-200">{{ cdom.cname_target }}</span><CopyButton :text="cdom.cname_target" sm label="Copy CNAME value" /></div></td>
                      </tr>
                      <tr>
                        <td class="py-2 pr-3 mono text-zinc-200">TXT</td>
                        <td class="py-2 pr-3"><div class="flex items-center gap-1"><span class="mono break-all text-zinc-200">{{ cdom.challenge_host }}</span><CopyButton :text="cdom.challenge_host" sm label="Copy TXT host name" /></div></td>
                        <td class="py-2"><div class="flex items-center gap-1"><span class="mono break-all text-zinc-200">{{ cdom.challenge_token }}</span><CopyButton :text="cdom.challenge_token" sm label="Copy TXT value" /></div></td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <p v-if="isApex" class="mt-2 text-xs text-zinc-400">
                  This is a root (apex) domain. Many DNS providers don't allow a CNAME at the root; use an ALIAS or ANAME record, or CNAME flattening, if your provider offers it.
                </p>
                <p v-if="cdom.error" role="alert" class="mt-2 wrap-break-word text-amber-300">We can't see the TXT record yet. DNS can take a few minutes.</p>
                <p class="mt-2 text-xs text-zinc-400">
                  We re-check every 20 seconds for a few minutes. DNS changes usually land within minutes but can take up to 48 hours,
                  so if it doesn't verify yet, come back and click Verify DNS.
                </p>
              </div>

              <div class="flex flex-wrap gap-2">
                <button v-if="cdom.status !== 'active'" type="button" class="btn-secondary" :disabled="verifyBusy || suspended" @click="verifyDomain()">{{ verifyBusy ? 'Checking…' : 'Verify DNS' }}</button>
                <span v-if="cdom.status !== 'active' && domainPolling" class="meta self-center" role="status">Re-checking every 20s…</span>
              </div>
            </div>
          </section>

          <section v-if="isAdmin" class="card card-pad" aria-labelledby="h-sub">
            <h2 id="h-sub" class="section-title mb-1">Short address <span class="badge-info ml-1 align-middle">Admin</span></h2>
            <p class="help">
              Also serve this app at a short address on the platform domain, covered by the platform certificate.
            </p>
            <form class="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center" @submit.prevent="saveSub">
              <label for="sub-input" class="sr-only">Short address subdomain</label>
              <input id="sub-input" v-model="subInput" placeholder="blog" class="input sm:max-w-xs" autocomplete="off" autocapitalize="none" spellcheck="false"
                :aria-invalid="subError ? 'true' : 'false'" :aria-describedby="subError ? 'sub-err' : undefined" :disabled="subBusy" @input="subError = ''" />
              <div class="flex gap-2">
                <button type="submit" class="btn-secondary" :disabled="subBusy || !subInput.trim() || subInput.trim().toLowerCase() === app.platform_subdomain">{{ subBusy ? 'Saving…' : 'Save' }}</button>
                <button v-if="app.platform_subdomain" type="button" class="btn-secondary" :disabled="subBusy" @click="clearSub">Clear</button>
              </div>
            </form>
            <p v-if="app.platform_host" class="help mt-2 break-all">Currently <span class="mono">{{ app.platform_host }}</span></p>
            <p v-if="subError" id="sub-err" role="alert" class="field-error mt-2 wrap-break-word">{{ subError }}</p>
          </section>

          <section v-if="canMove" class="card card-pad" aria-labelledby="h-move">
            <h2 id="h-move" class="section-title mb-1">Project</h2>
            <p class="help mt-0! mb-2">Currently in {{ fromProject?.is_personal ? 'Personal' : fromProject?.name }}. Moving keeps the URL, data and credentials.</p>
            <form class="flex flex-col gap-2 sm:flex-row sm:items-center" @submit.prevent="onMove">
              <label for="move-to" class="sr-only">Move to project</label>
              <select id="move-to" v-model="moveTo" class="input sm:max-w-xs">
                <option :value="null" disabled>Move to…</option>
                <option v-for="p in moveTargets" :key="p.id" :value="p.id">{{ p.is_personal ? 'Personal' : p.name }}</option>
              </select>
              <button type="submit" class="btn-secondary" :disabled="moveTo === null || moving || suspended" :aria-busy="moving">{{ moving ? 'Moving…' : 'Move app' }}</button>
            </form>
            <p v-if="moveError" role="alert" class="field-error mt-2 wrap-break-word">{{ moveError }}</p>
          </section>

          <!-- Extra settings sections go in this slot; the Danger zone must stay the last element. -->
          <slot />

          <section class="card card-pad border-danger/40" aria-labelledby="h-danger">
            <h2 id="h-danger" class="section-title text-danger-light">Danger zone</h2>
            <div class="mt-2 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <p class="text-sm text-zinc-300">
                Delete this app{{ hasAddons ? ', its database and blob storage bucket' : '' }}. This can't be undone.
              </p>
              <button type="button" class="btn-danger shrink-0" @click="deleteOpen = true">Delete app…</button>
            </div>
            <div v-if="hasAddons" class="mt-3 flex flex-col gap-3 border-t border-pico-border pt-3 sm:flex-row sm:items-center sm:justify-between">
              <p class="min-w-0 text-sm text-zinc-300">
                Rotate credentials: new ones are generated and the old ones stop working immediately. The app restarts to pick them up, so expect a short interruption.
              </p>
              <div class="flex shrink-0 flex-wrap gap-2">
                <button v-if="app.want_db" type="button" class="btn-secondary" :disabled="suspended" @click="rotating = 'db'">Rotate database password</button>
                <button v-if="app.want_storage" type="button" class="btn-secondary" :disabled="suspended" @click="rotating = 'storage'">Rotate blob storage keys</button>
              </div>
            </div>
            <div v-if="cdom" class="mt-3 flex flex-col gap-3 border-t border-pico-border pt-3 sm:flex-row sm:items-center sm:justify-between">
              <p class="min-w-0 text-sm text-zinc-300">
                Remove <span class="mono break-all">{{ cdom.host }}</span>. Its certificate is dropped and the app stays at {{ app.hostname }}.
              </p>
              <button type="button" class="btn-danger shrink-0" :disabled="clearingDomain || suspended" @click="removeDomainOpen = true">Remove domain…</button>
            </div>
          </section>

        </div>
    <ConfirmDialog
      :open="rotating !== null"
      :title="rotating === 'db' ? 'Rotate database password' : 'Rotate blob storage keys'"
      message="New credentials are generated and the old ones stop working. The app restarts to pick them up."
      confirm-label="Rotate"
      busy-label="Rotating…"
      danger
      :busy="rotateBusy"
      @confirm="confirmRotate"
      @cancel="rotating = null"
    />
    <ConfirmDialog
      :open="removeDomainOpen && !!cdom"
      title="Remove custom domain"
      message="The domain stops serving this app and its certificate is removed. Your DNS records stay as they are."
      confirm-label="Remove domain"
      busy-label="Removing…"
      danger
      :busy="clearingDomain"
      :require-text="cdom?.host"
      @confirm="removeDomain"
      @cancel="removeDomainOpen = false"
    />
    <DeleteAppDialog :app="deleteOpen ? app : null" :busy="deleting" @confirm="confirmDelete" @cancel="deleteOpen = false" />
  </div>
</template>
