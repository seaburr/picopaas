<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()

async function onLogout() {
  await auth.logout()
  router.push('/login')
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center bg-pico-bg px-4">
    <div class="w-full max-w-sm rounded-lg border border-pico-border bg-pico-panel p-6">
      <template v-if="auth.user?.status === 'suspended'">
        <h1 class="text-lg font-semibold text-white">Account suspended</h1>
        <p class="mt-2 text-sm text-zinc-400">
          Your account has been suspended by an administrator and your apps are stopped. Contact an administrator.
        </p>
      </template>
      <template v-else>
        <h1 class="text-lg font-semibold text-white">Awaiting approval</h1>
        <p class="mt-2 text-sm text-zinc-400">
          Your account ({{ auth.user?.email }}) has been created, but an administrator must approve it before you
          can deploy apps. Check back later.
        </p>
      </template>
      <button
        type="button"
        class="mt-5 rounded border border-pico-border px-3 py-1.5 text-sm text-zinc-300 hover:bg-pico-card"
        @click="onLogout"
      >
        Log out
      </button>
    </div>
  </div>
</template>
