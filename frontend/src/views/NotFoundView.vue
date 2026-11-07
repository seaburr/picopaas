<script setup lang="ts">
import { useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
</script>

<template>
  <main id="main" tabindex="-1" class="page-main">
    <div class="max-w-md py-8">
      <p class="meta mono">404</p>
      <h1 class="page-title mt-1">Page not found</h1>
      <p class="mt-2 wrap-break-word text-sm text-zinc-300">
        There's nothing at <code class="mono text-white">{{ route.path }}</code>. The link may be old or mistyped.
      </p>
      <div class="mt-4 flex gap-2">
        <RouterLink v-if="auth.user" to="/apps" class="btn-primary">Go to your apps</RouterLink>
        <RouterLink v-else to="/login" class="btn-primary">Log in</RouterLink>
        <RouterLink to="/guide/user" class="btn-secondary">Read the docs</RouterLink>
      </div>
    </div>
  </main>
</template>
