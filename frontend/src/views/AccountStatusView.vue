<script setup lang="ts">
import IconButton from '../components/IconButton.vue'
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import BrandMark from '../components/BrandMark.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const checking = ref(false)
let timer: number | undefined

async function check() {
  checking.value = true
  await auth.refresh()
  checking.value = false
  if (!auth.user) router.push('/login')
  else if (auth.user.status === 'active') router.push('/apps')
}
onMounted(() => (timer = window.setInterval(check, 20000)))
onBeforeUnmount(() => window.clearInterval(timer))

async function onLogout() {
  await auth.logout()
  router.push('/login')
}
</script>

<template>
  <div class="flex min-h-[calc(100vh-2rem)] items-center justify-center px-4 py-8">
    <main id="main" tabindex="-1" class="page-narrow w-full"><div class="mx-auto w-full max-w-md">
      <div class="mb-4 text-center"><BrandMark /></div>
      <div v-if="auth.user?.status === 'suspended'" class="border-t border-pico-border pt-3" role="status">
        <span class="st tone-err text-xs font-medium"><span class="dot dot-err" aria-hidden="true" />Suspended</span>
        <h1 class="page-title mt-2">Your account is suspended</h1>
        <p class="mt-2 text-sm text-zinc-300">
          An administrator suspended <strong class="mono wrap-break-word text-white">{{ auth.user?.email }}</strong> and your
          apps are stopped. Your data is kept. Ask the administrator to restore your account.
        </p>
      </div>
      <div v-else class="border-t border-pico-border pt-3" role="status">
        <span class="st tone-warn text-xs font-medium"><span class="dot dot-warn" aria-hidden="true" />Waiting for approval</span>
        <h1 class="page-title mt-2">Thanks, you're on the list</h1>
        <p class="mt-2 text-sm text-zinc-300">
          We've received your request for <strong class="mono wrap-break-word text-white">{{ auth.user?.email }}</strong>. If
          your account is approved, a PicoPaaS platform admin will reach out, usually within a day. You don't need to do anything: this page checks
          every 20 seconds and takes you to your apps once you're approved.
        </p>
        <IconButton class="mt-3" text="Check now" :label="checking ? 'Checking…' : 'Check now'" :busy="checking" @click="check" />
      </div>
      <p class="mt-4 flex justify-center gap-4 text-sm">
        <RouterLink to="/guide" class="link">Docs</RouterLink>
        <button type="button" class="link" @click="onLogout">Log out</button>
      </p>
    </div></main>
  </div>
</template>
