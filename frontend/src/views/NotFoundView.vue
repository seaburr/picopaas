<script setup lang="ts">
import { useRoute } from 'vue-router'
import NavBar from '../components/NavBar.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
</script>

<template>
  <div class="min-h-screen">
    <NavBar />
    <main id="main" tabindex="-1" class="mx-auto max-w-md px-4 py-16 text-center">
      <p class="meta tracking-widest">404</p>
      <h1 class="page-title mt-2">Page not found</h1>
      <p class="mt-3 wrap-break-word text-sm text-zinc-300">
        There's nothing at <code class="text-white">{{ route.path }}</code>. The link may be old or mistyped.
      </p>
      <div class="mt-6 flex justify-center gap-3">
        <RouterLink v-if="auth.user" to="/apps" class="btn-primary">Go to your apps</RouterLink>
        <RouterLink v-else to="/login" class="btn-primary">Log in</RouterLink>
        <RouterLink to="/guide/user" class="btn-secondary">Read the docs</RouterLink>
      </div>
    </main>
  </div>
</template>
