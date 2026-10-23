<script setup lang="ts">
import { onErrorCaptured, ref, watch } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import { useAuthStore } from './stores/auth'
import DevBanner from './components/DevBanner.vue'
import PasswordBanner from './components/PasswordBanner.vue'
import ToastHost from './components/ToastHost.vue'

// Last-resort boundary: a view that throws while rendering shows this instead of a blank page.
const crashed = ref(false)
onErrorCaptured((err) => {
  console.error(err)
  crashed.value = true
  return false
})
const route = useRoute()
const auth = useAuthStore()
watch(() => route.fullPath, () => (crashed.value = false))
const reload = () => window.location.reload()
</script>

<template>
  <DevBanner slim />
  <PasswordBanner />
  <div v-if="crashed" role="alert" class="mx-auto max-w-md px-4 py-16 text-center">
    <h1 class="page-title">Something went wrong</h1>
    <p class="help">The page failed to load. Reloading usually fixes it.</p>
    <button type="button" class="btn-primary mt-4" @click="reload">Reload</button>
  </div>
  <RouterView v-else />
  <footer v-if="auth.user" class="mx-auto flex max-w-6xl flex-wrap justify-center gap-x-4 gap-y-1 px-4 py-6 text-xs text-zinc-500">
    <a href="/privacy" class="hover:text-zinc-300">Privacy</a>
    <RouterLink to="/guide" class="hover:text-zinc-300">Docs</RouterLink>
    <a href="https://blog.picopaas.xyz" target="_blank" rel="noopener" class="hover:text-zinc-300">Blog</a>
  </footer>
  <ToastHost />
</template>
