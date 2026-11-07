<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'
import { dismissOnboarding } from '../api/auth'
import { useModal } from '../composables/useModal'
import { markSeen, seenMemory, seenThisSession, useOnboarding } from '../composables/useOnboarding'

// Shown once per browser session after sign-in, on app pages, to active users who have not ticked "Don't show this again".
const auth = useAuthStore()
const toast = useToastStore()
const route = useRoute()
const router = useRouter()
const { forced } = useOnboarding()

const dlg = ref<HTMLDialogElement | null>(null)
const never = ref(false)
const busy = ref(false)
const titleId = `${useId()}-title`

const open = computed(() => {
  if (forced.value) return !!auth.user
  const u = auth.user
  if (!u || u.status !== 'active' || u.onboarding_dismissed !== false) return false
  if (!route.meta.requiresAuth || route.name === 'notfound') return false
  return !seenMemory.value && !seenThisSession()
})
watch(open, (v) => v && (never.value = false))

async function close() {
  if (busy.value) return
  if (never.value && auth.user) {
    busy.value = true
    try {
      await dismissOnboarding()
      auth.user.onboarding_dismissed = true
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Could not save this choice')
    } finally {
      busy.value = false
    }
  }
  markSeen()
  seenMemory.value = true
  forced.value = false
}
const m = useModal(dlg, () => open.value, close)

async function go(to: string) {
  await close()
  router.push(to)
}
</script>

<template>
  <dialog
    ref="dlg"
    :aria-labelledby="titleId"
    class="m-auto w-[calc(100%-2rem)] max-w-lg rounded-md border border-pico-border bg-pico-panel p-0 text-white shadow-lg backdrop:bg-black/70"
    @cancel="m.onNativeCancel"
    @click="m.onBackdrop"
  >
    <div class="p-4">
      <h2 :id="titleId" class="section-title">Getting started</h2>
      <ol class="mt-3 list-decimal space-y-2 pl-5 text-sm text-zinc-300">
        <li>The left tree is your map. Projects hold your apps (and Kaisen builds). Templates and Docs are linked below them.</li>
        <li>
          Deploy your first app: pick a template, or choose New app and give it a public image, for example
          <span class="mono">nginxinc/nginx-unprivileged:alpine</span> on port <span class="mono">8080</span>.
        </li>
        <li>Every app gets a URL and a health page with logs, usage and versions. Settings holds environment variables, add-ons, domain and backups.</li>
        <li>Need help? Docs has a guide for each feature.</li>
      </ol>
      <label v-if="!auth.user?.onboarding_dismissed" class="mt-4 flex items-center gap-2 text-sm text-zinc-300">
        <input v-model="never" type="checkbox" /> Don't show this again
      </label>
      <div class="mt-4 flex flex-col-reverse gap-2 border-t border-pico-border pt-3 sm:flex-row sm:justify-end">
        <button type="button" class="btn-secondary" :disabled="busy" @click="close">Dismiss</button>
        <button type="button" class="btn-secondary" :disabled="busy" @click="go('/guide/user#0-getting-started')">Read the guide</button>
        <button type="button" class="btn-primary" data-autofocus :disabled="busy" @click="go('/templates')">Deploy your first app</button>
      </div>
    </div>
  </dialog>
</template>
