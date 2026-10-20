<script setup lang="ts">
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
    <div class="w-full max-w-md">
      <div class="mb-5 flex justify-center"><BrandMark :size="40" /></div>
      <div v-if="auth.user?.status === 'suspended'" class="card card-pad border-danger/40" role="status">
        <span class="badge-error">Suspended</span>
        <h1 class="page-title mt-3">Your account is suspended</h1>
        <p class="mt-2 text-sm text-zinc-300">
          An administrator suspended <strong class="break-words text-white">{{ auth.user?.email }}</strong> and your
          apps are stopped. Your data is kept. Ask the administrator to restore your account.
        </p>
      </div>
      <div v-else class="card card-pad" role="status">
        <span class="badge-warn">Waiting for approval</span>
        <h1 class="page-title mt-3">Thanks, you're on the list</h1>
        <p class="mt-2 text-sm text-zinc-300">
          We've received your request for <strong class="break-words text-white">{{ auth.user?.email }}</strong>. If
          your account is approved, a PicoPaaS platform admin will reach out, usually within a day. You don't need to do anything: this page checks
          every 20 seconds and takes you to your apps once you're approved.
        </p>
        <button type="button" class="btn-secondary btn-sm mt-4" :disabled="checking" @click="check">
          {{ checking ? 'Checking…' : 'Check now' }}
        </button>
      </div>
      <p class="mt-4 flex justify-center gap-4 text-sm">
        <RouterLink to="/guide" class="link">Docs</RouterLink>
        <button type="button" class="link" @click="onLogout">Log out</button>
      </p>
    </div>
  </div>
</template>
