<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listUsers } from '../api/admin'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const pending = ref(0)
async function refreshPending() {
  if (auth.user?.role !== 'uberadmin') return
  try {
    pending.value = (await listUsers()).filter((u) => u.status === 'pending').length
  } catch {
    pending.value = 0
  }
}
onMounted(refreshPending)
watch(() => route.fullPath, refreshPending)

async function onLogout() {
  await auth.logout()
  router.push('/login')
}
</script>

<template>
  <header class="flex items-center justify-between border-b border-pico-border bg-pico-panel px-4 py-3">
    <div class="flex items-center gap-4">
      <RouterLink to="/" class="text-sm font-semibold text-white">PicoPaaS</RouterLink>
      <RouterLink v-if="auth.user?.role === 'uberadmin'" to="/apps" class="text-sm text-zinc-400 hover:text-white">My apps</RouterLink>
      <RouterLink to="/keys" class="text-sm text-zinc-400 hover:text-white">API keys</RouterLink>
      <RouterLink v-if="auth.user?.role === 'uberadmin'" to="/admin" class="text-sm text-zinc-400 hover:text-white">Admin<span v-if="pending > 0" class="ml-1 rounded-full bg-amber-500 px-1.5 text-xs font-semibold text-black">{{ pending }}</span></RouterLink>
      <RouterLink v-if="auth.user?.role === 'uberadmin'" to="/admin/health" class="text-sm text-zinc-400 hover:text-white">Health</RouterLink>
    </div>
    <div class="flex items-center gap-3 text-sm text-zinc-400">
      <a href="/privacy" class="hover:text-white">Privacy</a>
      <span v-if="auth.user" class="hidden sm:inline">{{ auth.user.email }}</span>
      <button type="button" class="rounded border border-pico-border px-2 py-1 hover:bg-pico-card" @click="onLogout">
        Log out
      </button>
    </div>
  </header>
</template>
