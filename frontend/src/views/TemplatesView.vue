<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import NavBar from '../components/NavBar.vue'
import { useAuthStore } from '../stores/auth'
import { ApiError } from '../api/client'
import { dbEngineLabel } from '../composables/appUi'
import { listTemplates, type AppTemplate } from '../api/templates'

const auth = useAuthStore()
const volumesOn = computed(() => auth.user?.features?.volumes === true)
const dbOff = computed(() => auth.user?.features?.databases === false)
const storageOff = computed(() => auth.user?.features?.storage === false)
const templates = ref<AppTemplate[]>([])
const loading = ref(true)
const error = ref('')
const offNotes = computed(() => {
  const n: string[] = []
  if (!volumesOn.value) n.push('persistent volumes')
  if (dbOff.value) n.push('databases')
  if (storageOff.value) n.push('blob storage')
  return n
})

// "Ghost (blog/CMS)" -> "Ghost"
const DOCS_NAME: Record<string, string> = { grafana: 'Grafana', 'grafana-pg': 'Grafana', status: 'Gatus', 'static-site': 'nginx' }
const docsName = (t: AppTemplate) => DOCS_NAME[t.id] ?? t.name.replace(/\s*\(.*\)$/, '')

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
    <main id="main" tabindex="-1" class="page-main">
      <nav class="meta mb-2" aria-label="Breadcrumb">
        <RouterLink to="/apps" class="link">Apps</RouterLink><span aria-hidden="true"> / </span>Templates
      </nav>
      <h1 class="page-title">Templates</h1>
      <p class="help mb-3">Pick a ready-made app. "Use this template" opens the new-app form prefilled with add-ons, environment variables, health check and volume, and you can change anything before creating. Secrets are listed but not generated: you set their values.</p>

      <p v-if="offNotes.length" class="alert-warn mb-3">Off on this instance: {{ offNotes.join(', ') }}. Templates that need them are marked below; templates with an optional volume deploy without it, and data on scratch disk is lost on restart.</p>

      <div v-if="loading" class="text-sm text-zinc-400" aria-busy="true">Loading…</div>
      <div v-else-if="error" role="alert" class="alert-error">{{ error }}</div>
      <div v-else class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <section v-for="t in templates" :key="t.id" class="card card-pad flex min-w-0 flex-col gap-2">
          <h2 class="section-title wrap-break-word">{{ t.name }}</h2>
          <p class="text-sm text-zinc-300">{{ t.description }}</p>
          <div class="flex flex-wrap gap-1">
            <span v-if="t.wants_db" class="badge badge-info">{{ dbEngineLabel(t.db_engine) }}</span>
            <span v-if="t.wants_storage" class="badge badge-info">Object Storage</span>
            <span v-if="t.volume" class="badge badge-info">Block Storage{{ t.volume.required ? '' : ' (optional)' }}</span>
            <span v-if="!t.wants_db && !t.wants_storage && !t.volume" class="badge badge-neutral">No add-ons</span>
            <span v-if="t.secrets.length" class="badge badge-neutral">{{ t.secrets.length }} secret{{ t.secrets.length === 1 ? '' : 's' }}</span>
          </div>
          <p v-if="t.volume && !volumesOn" class="meta">{{ t.volume.required ? 'Needs a persistent volume, which is off here.' : 'Deploys without the optional volume.' }}</p>
          <p v-if="(t.wants_db && dbOff) || (t.wants_storage && storageOff)" class="meta">Needs an add-on that is off here.</p>
          <p class="mono text-xs text-zinc-400"><span class="break-all">{{ t.image }}</span> <span class="whitespace-nowrap">· port {{ t.port }}</span></p>
          <div class="mt-auto pt-2">
            <RouterLink :to="{ path: '/apps/new', query: { template: t.id } }" class="btn-primary">Use this template</RouterLink>
            <p class="meta mt-2 flex flex-wrap gap-x-3">
              <a v-if="t.docs_url" :href="t.docs_url" target="_blank" rel="noopener noreferrer" class="link">Setup guide</a>
              <a v-if="t.upstream_docs_url" :href="t.upstream_docs_url" target="_blank" rel="noopener noreferrer" class="link">{{ docsName(t) }} docs</a>
            </p>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>
