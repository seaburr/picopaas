<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
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
    await router.replace(`/projects/${p.id}`)
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
  <main id="main" tabindex="-1" class="page-main">
    <div class="max-w-md py-8">
      <h1 class="page-title">Project invite</h1>
      <p v-if="busy" class="help" aria-busy="true">Joining the project…</p>
      <template v-else-if="error">
        <p role="alert" class="alert-error mt-3">{{ error }}</p>
        <div class="mt-3 flex gap-2">
          <button type="button" class="btn-secondary" @click="accept">Try again</button>
          <RouterLink :to="projects.appsHome" class="btn-primary">Go to apps</RouterLink>
        </div>
      </template>
    </div>
  </main>
</template>
