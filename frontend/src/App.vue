<script setup lang="ts">
import { computed, onErrorCaptured, ref, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'
import AppSidebar from './components/AppSidebar.vue'
import DevBanner from './components/DevBanner.vue'
import PasswordBanner from './components/PasswordBanner.vue'
import GettingStartedDialog from './components/GettingStartedDialog.vue'
import ToastHost from './components/ToastHost.vue'

// Last-resort boundary: a view that throws while rendering shows this instead of a blank page.
const crashed = ref(false)
onErrorCaptured((err) => {
  console.error(err)
  crashed.value = true
  return false
})
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
watch(() => route.fullPath, () => (crashed.value = false))
const reload = () => window.location.reload()

// The sidebar shell wraps every page except the centered sign-in / status pages. Wait for the first navigation so a
// signed-in reload does not flash the signed-out sidebar.
const ready = ref(false)
router.isReady().then(() => (ready.value = true))
const BARE = new Set(['/login', '/register', '/pending'])
const shell = computed(() => ready.value && !BARE.has(route.path))
</script>

<template>
  <div class="flex min-h-screen min-[760px]:flex-row max-[759px]:flex-col">
    <AppSidebar v-if="shell" />
    <div class="flex min-w-0 flex-1 flex-col">
      <DevBanner slim />
      <PasswordBanner />
      <div v-if="crashed" role="alert" class="mx-auto max-w-md px-4 py-16 text-center">
        <h1 class="page-title">Something went wrong</h1>
        <p class="help">The page failed to load. Reloading usually fixes it.</p>
        <button type="button" class="btn-primary mt-4" @click="reload">Reload</button>
      </div>
      <!-- Pages for one thing (/apps/:id, /kaisen/:id, ...) load it on mount; a fresh instance per id, so going from
           one app's Health to another's loads the other app instead of keeping the first. -->
      <div v-else class="flex-1"><RouterView v-slot="{ Component, route: r }"><component :is="Component" :key="r.params.id ? r.path : undefined" /></RouterView></div>
      <!-- Pages without the sidebar (account status): the legal links the sidebar foot would carry. -->
      <footer v-if="!shell && auth.user" class="mx-auto flex max-w-6xl flex-wrap justify-center gap-x-4 gap-y-1 px-4 py-6 text-xs text-zinc-400">
        <a href="/privacy" class="hover:text-white hover:underline" title="Privacy, terms of use and how to report abuse">Legal</a>
        <RouterLink to="/guide" class="hover:text-white hover:underline">Docs</RouterLink>
        <a href="https://blog.picopaas.xyz" target="_blank" rel="noopener" class="hover:text-white hover:underline">Blog</a>
      </footer>
    </div>
  </div>
  <GettingStartedDialog />
  <ToastHost />
</template>
