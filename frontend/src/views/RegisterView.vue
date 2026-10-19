<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()

const form = reactive({ email: '', password: '' })
const error = ref('')
const loading = ref(false)

async function onSubmit() {
  error.value = ''
  loading.value = true
  try {
    await auth.register(form.email, form.password)
    await router.push('/')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Registration failed'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center bg-pico-bg px-4">
    <div class="w-full max-w-sm">
      <h1 class="mb-6 text-center text-xl font-bold text-white">PicoPaaS</h1>
      <div class="rounded-lg border border-pico-border bg-pico-panel p-6">
        <form class="space-y-4" @submit.prevent="onSubmit">
          <div>
            <label class="mb-1 block text-sm font-medium text-zinc-300">Email</label>
            <input
              v-model="form.email"
              type="email"
              required
              autocomplete="username"
              class="w-full rounded-md border border-pico-border bg-pico-card px-3 py-2 text-white placeholder-zinc-500 focus:outline-none focus:border-accent"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium text-zinc-300">Password</label>
            <input
              v-model="form.password"
              type="password"
              required
              minlength="8"
              autocomplete="new-password"
              placeholder="min 8 characters"
              class="w-full rounded-md border border-pico-border bg-pico-card px-3 py-2 text-white placeholder-zinc-500 focus:outline-none focus:border-accent"
            />
          </div>
          <div v-if="error" class="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {{ error }}
          </div>
          <button
            type="submit"
            :disabled="loading"
            class="w-full rounded-md bg-accent-dark px-4 py-2 font-semibold text-white hover:bg-accent disabled:opacity-50"
          >
            {{ loading ? 'Creating account…' : 'Create account' }}
          </button>
        </form>
        <p class="mt-4 text-center text-sm text-zinc-500">
          Already have an account? <RouterLink to="/login" class="text-accent-light hover:underline">Sign in</RouterLink>
        </p>
      </div>
    </div>
  </div>
</template>
