<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import NavBar from '../components/NavBar.vue'
import { useAuthStore } from '../stores/auth'
import { ApiError } from '../api/client'
import { listTemplates, type AppTemplate } from '../api/templates'

const auth = useAuthStore()
const volumesOn = computed(() => auth.user?.features?.volumes === true)
const dbOff = computed(() => auth.user?.features?.databases === false)
const storageOff = computed(() => auth.user?.features?.storage === false)
const templates = ref<AppTemplate[]>([])
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  try {
    templates.value = await listTemplates()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load templates."
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main id="main" tabindex="-1" class="mx-auto max-w-6xl px-4 py-4 sm:px-6">
      <nav class="meta mb-2" aria-label="Breadcrumb">
        <RouterLink to="/apps" class="link">Apps</RouterLink><span aria-hidden="true"> / </span>Templates
      </nav>
      <h1 class="page-title">Templates</h1>
      <p class="help mb-3">Pick a ready-made app. Use this template opens the new-app form prefilled (add-ons, environment variables, health check, volume); you can change anything before creating. Secrets are listed, not generated: you set their values.</p>

      <div v-if="loading" class="text-sm text-zinc-400" aria-busy="true">Loading…</div>
      <div v-else-if="error" role="alert" class="alert-error">{{ error }}</div>
      <div v-else class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <section v-for="t in templates" :key="t.id" class="card card-pad flex min-w-0 flex-col gap-2">
          <h2 class="section-title wrap-break-word">{{ t.name }}</h2>
          <p class="text-sm text-zinc-300">{{ t.description }}</p>
          <div class="flex flex-wrap gap-1">
            <span v-if="t.wants_db" class="badge badge-info">{{ t.db_engine === 'pg' ? 'PostgreSQL database' : 'MySQL database' }}</span>
            <span v-if="t.wants_storage" class="badge badge-info">Blob storage bucket</span>
            <span v-if="t.volume" class="badge badge-info">{{ t.volume.size_gi }} GiB volume{{ t.volume.required ? '' : ' (optional)' }}</span>
            <span v-if="!t.wants_db && !t.wants_storage && !t.volume" class="badge badge-neutral">No add-ons</span>
            <span v-if="t.secrets.length" class="badge badge-neutral">{{ t.secrets.length }} secret{{ t.secrets.length === 1 ? '' : 's' }} to set</span>
          </div>
          <p v-if="t.volume && !volumesOn" :class="t.volume.required ? 'alert-warn' : 'meta'">
            Persistent volumes are off on this instance.
            {{ t.volume.required ? 'This template needs one.' : 'It deploys without the volume; data on scratch disk is lost on restart.' }}
          </p>
          <p v-if="t.wants_db && dbOff" class="alert-warn">Databases are off on this instance.</p>
          <p v-if="t.wants_storage && storageOff" class="alert-warn">Blob storage is off on this instance.</p>
          <p class="mono text-xs text-zinc-400"><span class="break-all">{{ t.image }}</span> <span class="whitespace-nowrap">· port {{ t.port }}</span></p>
          <p v-if="t.notes" class="meta">{{ t.notes }}</p>
          <div class="mt-auto flex items-center justify-between gap-2 pt-2">
            <a v-if="t.docs_url" :href="t.docs_url" target="_blank" rel="noopener noreferrer" class="link text-sm">README on GitHub</a>
            <span v-else />
            <RouterLink :to="{ path: '/apps/new', query: { template: t.id } }" class="btn-primary shrink-0">Use this template</RouterLink>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>
