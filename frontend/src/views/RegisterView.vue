<script setup lang="ts">
import { reactive, ref, useId } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError } from '../api/client'
import BrandMark from '../components/BrandMark.vue'
import PasswordField from '../components/PasswordField.vue'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const emailId = useId()
const acceptId = useId()

const form = reactive({ email: '', password: '', accept: false })
const error = ref('')
const fieldErrors = ref<Record<string, string>>({})
const loading = ref(false)

async function onSubmit() {
  error.value = ''
  fieldErrors.value = {}
  loading.value = true
  try {
    const hasSession = await auth.register(form.email, form.password, form.accept)
    // Same 201 for new and existing emails (no session for an existing one): never reveal which.
    if (hasSession) await router.push('/')
    else await router.push({ path: '/login', query: { registered: '1' } })
  } catch (e) {
    if (e instanceof ApiError) {
      error.value = e.message
      fieldErrors.value = e.fieldErrors
    } else error.value = "Couldn't send your request. Try again."
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="flex min-h-[calc(100vh-2rem)] items-center justify-center px-4 py-8">
    <div class="w-full max-w-sm">
      <h1 class="mb-1 flex justify-center"><BrandMark :size="40" /></h1>
      <p class="mb-5 text-center text-sm text-zinc-300">Tiny hosting for throwaway projects.</p>

      <div class="card card-pad">
        <h2 class="section-title mb-1">Request access</h2>
        <p class="help mb-4 mt-0">Sign up, and if your account is approved, a PicoPaaS platform admin will reach out, usually within a day.</p>
        <form class="space-y-4" @submit.prevent="onSubmit">
          <div>
            <label :for="emailId" class="label">Email</label>
            <input :id="emailId" v-model="form.email" type="email" required autocomplete="username" class="input" :aria-invalid="fieldErrors.email ? 'true' : undefined" />
            <p v-if="fieldErrors.email" class="field-error">{{ fieldErrors.email }}</p>
          </div>
          <PasswordField
            v-model="form.password"
            autocomplete="new-password"
            :minlength="8"
            help="At least 8 characters."
            :invalid="!!fieldErrors.password"
          />
          <p v-if="fieldErrors.password" class="field-error -mt-3">{{ fieldErrors.password }}</p>
          <div class="flex items-start gap-2 text-sm text-zinc-300">
            <input :id="acceptId" v-model="form.accept" type="checkbox" required class="check mt-0.5" />
            <label :for="acceptId">
              I agree to the
              <a href="/privacy" target="_blank" rel="noopener" class="link">Privacy Policy &amp; Acceptable Use</a>
            </label>
          </div>
          <div v-if="error" class="alert-error whitespace-pre-line" role="alert">{{ error }}</div>
          <button type="submit" :disabled="loading || !form.accept" class="btn-primary btn-block">
            {{ loading ? 'Sending request…' : 'Request access' }}
          </button>
        </form>
        <p class="mt-4 text-center text-sm text-zinc-300">
          Have an account? <RouterLink to="/login" class="link">Log in</RouterLink>
        </p>
      </div>
      <p class="mt-4 text-center text-xs text-zinc-400">
        <RouterLink to="/guide" class="link">Docs</RouterLink> · <a href="/privacy" class="link">Privacy</a> · <a href="https://status.picopaas.xyz" target="_blank" rel="noopener noreferrer" class="link">Status</a>
      </p>
    </div>
  </div>
</template>
