<script setup lang="ts">
import { reactive, ref, useId } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '../api/client'
import BrandMark from '../components/BrandMark.vue'
import PasswordField from '../components/PasswordField.vue'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const emailId = useId()

const form = reactive({ email: '', password: '' })
const error = ref('')
const loading = ref(false)

async function onSubmit() {
  error.value = ''
  loading.value = true
  try {
    await auth.login(form.email, form.password)
    // ?next= is only honoured for same-site paths (no protocol-relative or absolute URLs).
    const next = typeof route.query.next === 'string' ? route.query.next : ''
    await router.push(next.startsWith('/') && !next.startsWith('//') ? next : '/')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't log you in. Try again."
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center px-4 py-8">
    <main id="main" tabindex="-1" class="page-narrow w-full"><div class="mx-auto w-full max-w-xs">
      <div class="mb-3 text-center"><BrandMark /></div>
      <h1 class="mb-4 text-center text-sm text-zinc-300">Log in to PicoPaaS</h1>
      <form class="space-y-3" @submit.prevent="onSubmit">
        <div>
          <label :for="emailId" class="label">Email</label>
          <input :id="emailId" v-model="form.email" type="email" required autocomplete="username" class="input" :aria-invalid="error ? 'true' : undefined" />
        </div>
        <PasswordField v-model="form.password" :invalid="!!error" />
        <div v-if="route.query.registered && !error" class="alert-info" role="status">
          If you already have an account, log in below. Otherwise, if your account is approved, a PicoPaaS platform admin will reach out, usually within a day.
        </div>
        <div v-if="error" class="alert-error" role="alert">{{ error }}</div>
        <button type="submit" :disabled="loading" class="btn-primary btn-block">
          {{ loading ? 'Logging in…' : 'Log in' }}
        </button>
      </form>
      <div class="mt-4 border-t border-pico-border pt-3 text-center">
        <p class="help mt-0">Forgot your password? Ask the administrator to reset it.</p>
        <p class="mt-2 text-sm text-zinc-300">
          Need access? <RouterLink to="/register" class="link">Request access</RouterLink>
        </p>
        <p class="mt-3 text-xs text-zinc-400">
          <RouterLink to="/guide" class="link">Docs</RouterLink> · <a href="/privacy" class="link">Privacy</a>
        </p>
      </div>
    </div></main>
  </div>
</template>
