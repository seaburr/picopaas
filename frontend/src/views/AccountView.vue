<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import NavBar from '../components/NavBar.vue'
import PasswordField from '../components/PasswordField.vue'
import { changePassword } from '../api/auth'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'
import { fullDate } from '../utils/format'

const auth = useAuthStore()
const toast = useToastStore()
const form = reactive({ current: '', next: '', confirm: '' })
const error = ref('')
const loading = ref(false)
const mismatch = computed(() => form.confirm !== '' && form.confirm !== form.next)

async function onSubmit() {
  error.value = ''
  if (form.next !== form.confirm) {
    error.value = "The new passwords don't match."
    return
  }
  loading.value = true
  try {
    await changePassword(form.current, form.next)
    form.current = form.next = form.confirm = ''
    if (auth.user) auth.user.must_change_password = false
    toast.success('Password changed.')
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) error.value = "Changing your password isn't available on this server yet. Ask the administrator to reset it."
    else error.value = e instanceof ApiError ? e.message : "Couldn't change your password. Try again."
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main id="main" tabindex="-1" class="mx-auto max-w-2xl space-y-6 px-4 py-6 sm:px-6">
      <h1 class="page-title">Account</h1>

      <section class="card card-pad" aria-label="Profile">
        <dl class="grid grid-cols-[7rem_1fr] gap-y-2 text-sm">
          <dt class="text-zinc-400">Email</dt>
          <dd class="break-all text-white">{{ auth.user?.email }}</dd>
          <dt class="text-zinc-400">Role</dt>
          <dd class="text-white">{{ auth.user?.role === 'uberadmin' ? 'Administrator' : 'User' }}</dd>
          <dt class="text-zinc-400">App limit</dt>
          <dd class="text-white">{{ auth.user?.app_limit }}</dd>
          <dt class="text-zinc-400">Joined</dt>
          <dd class="text-white">{{ auth.user ? fullDate(auth.user.created_at) : '' }}</dd>
        </dl>
      </section>

      <section class="card card-pad" aria-labelledby="pw-title">
        <h2 id="pw-title" class="section-title">Change password</h2>
        <form class="mt-4 max-w-sm space-y-4" @submit.prevent="onSubmit">
          <PasswordField v-model="form.current" label="Current password" autocomplete="current-password" />
          <PasswordField v-model="form.next" label="New password" autocomplete="new-password" :minlength="8" help="At least 8 characters." />
          <div>
            <PasswordField v-model="form.confirm" label="Confirm new password" autocomplete="new-password" :minlength="8" :invalid="mismatch" />
            <p v-if="mismatch" class="field-error">Passwords don't match.</p>
          </div>
          <div v-if="error" class="alert-error" role="alert">{{ error }}</div>
          <button type="submit" class="btn-primary" :disabled="loading || mismatch">{{ loading ? 'Saving…' : 'Change password' }}</button>
        </form>
      </section>
    </main>
  </div>
</template>
