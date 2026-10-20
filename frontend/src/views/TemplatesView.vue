<script setup lang="ts">
import { onMounted, ref } from 'vue'
import NavBar from '../components/NavBar.vue'
import { ApiError } from '../api/client'
import { listTemplates, type AppTemplate } from '../api/templates'

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
    <main id="main" tabindex="-1" class="mx-auto max-w-4xl px-4 py-4">
      <nav class="meta mb-2" aria-label="Breadcrumb">
        <RouterLink to="/apps" class="link">Apps</RouterLink><span aria-hidden="true"> / </span>Templates
      </nav>
      <h1 class="page-title">Templates</h1>
      <p class="help mb-3">Pick a ready-made app. Deploy opens the new-app form prefilled; you can change anything before creating.</p>

      <div v-if="loading" class="text-sm text-zinc-400" aria-busy="true">Loading…</div>
      <div v-else-if="error" role="alert" class="alert-error">{{ error }}</div>
      <div v-else class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <section v-for="t in templates" :key="t.id" class="card card-pad flex min-w-0 flex-col gap-2">
          <h2 class="section-title break-words">{{ t.name }}</h2>
          <p class="text-sm text-zinc-300">{{ t.description }}</p>
          <div class="flex flex-wrap gap-1">
            <span v-if="t.wants_db" class="badge badge-info">MySQL database</span>
            <span v-if="t.wants_storage" class="badge badge-info">Storage bucket</span>
            <span v-if="!t.wants_db && !t.wants_storage" class="badge badge-neutral">No add-ons</span>
          </div>
          <p class="mono break-all text-xs text-zinc-400">{{ t.image }} · port {{ t.port }}</p>
          <p v-if="t.notes" class="meta">{{ t.notes }}</p>
          <div class="mt-auto flex items-center justify-between gap-2 pt-2">
            <RouterLink v-if="t.docs_url" :to="t.docs_url" target="_blank" class="link text-sm">Docs</RouterLink>
            <span v-else />
            <RouterLink :to="{ path: '/apps/new', query: { template: t.id } }" class="btn-primary">Deploy</RouterLink>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>
