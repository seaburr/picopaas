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
  <header class="flex items-center justify-between border-b border-pico-border bg-pico-panel px-4 py-3">
    <div class="flex items-center gap-4">
      <RouterLink to="/" class="text-sm font-semibold text-white">PicoPaaS</RouterLink>
      <RouterLink to="/keys" class="text-sm text-zinc-400 hover:text-white">API keys</RouterLink>
      <RouterLink v-if="auth.user?.role === 'uberadmin'" to="/admin" class="text-sm text-zinc-400 hover:text-white">Admin</RouterLink>
    </div>
    <div class="flex items-center gap-3 text-sm text-zinc-400">
      <span v-if="auth.user">{{ auth.user.email }}</span>
      <button type="button" class="rounded border border-pico-border px-2 py-1 hover:bg-pico-card" @click="onLogout">
        Log out
      </button>
    </div>
  </header>
</template>
