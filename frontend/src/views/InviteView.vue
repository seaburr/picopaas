<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import NavBar from '../components/NavBar.vue'
import { ApiError } from '../api/client'
import { acceptInvite } from '../api/projects'
import { useProjectsStore } from '../stores/projects'
import { useToastStore } from '../stores/toast'

// /invite/:token. The router guard sends logged-out visitors through /login?next=/invite/<token> and back here.
const props = defineProps<{ token: string }>()
const router = useRouter()
const projects = useProjectsStore()
const toast = useToastStore()
const error = ref('')
const busy = ref(true)

async function accept() {
  busy.value = true
  error.value = ''
  try {
    const p = await acceptInvite(props.token)
    projects.upsert(p)
    projects.select(p.id)
    await projects.load()
    toast.success(`Joined ${p.name}.`)
    await router.replace('/apps')
  } catch (e) {
    error.value =
      e instanceof ApiError && (e.status === 404 || e.status === 410)
        ? 'This invite link is invalid, already used, expired or revoked. Ask a project owner for a new one.'
        : e instanceof ApiError
          ? e.message
          : "Couldn't accept the invite."
  } finally {
    busy.value = false
  }
}
onMounted(accept)
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main id="main" tabindex="-1" class="mx-auto max-w-md px-4 py-10 text-center">
      <h1 class="page-title">Project invite</h1>
      <p v-if="busy" class="help" aria-busy="true">Joining the project…</p>
      <template v-else-if="error">
        <p role="alert" class="alert-error mt-3 text-left">{{ error }}</p>
        <div class="mt-4 flex justify-center gap-2">
          <button type="button" class="btn-secondary" @click="accept">Try again</button>
          <RouterLink to="/apps" class="btn-primary">Go to apps</RouterLink>
        </div>
      </template>
    </main>
  </div>
</template>
