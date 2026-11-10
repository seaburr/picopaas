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
      <h2 :id="titleId" class="section-title">Getting Started with PicoPaaS</h2>
      <div class="mt-3 space-y-2 text-sm text-zinc-300">
        <p>PicoPaaS is a platform to host applications simply. You can bring your app, use one of our pre-rolled app templates, or build an app conversationally using Kaisen.</p>
        <p>Create your first app in your personal project (see the left navigation tree) or create a new project if you want to invite other users to collaborate and manage.</p>
        <p>
          Once you're in the correct project click on "New app". Select "Deploy an image" if you've got your image URL handy and know it can run in
          PicoPaaS (see docs for platform limitations). If you want to deploy from our template library, select "Deploy from a template".
        </p>
        <p>
          Finally, if you want to build an application conversationally, add an Anthropic API key to the Kaisen settings page in the project, then
          select "Build with Kaisen" and start prompting your way to your app.
        </p>
        <p>Need help? Please review the docs for in depth guides, troubleshooting info, and everything else.</p>
      </div>
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
