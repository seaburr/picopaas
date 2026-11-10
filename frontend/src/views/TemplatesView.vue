<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useProjectsStore } from '../stores/projects'
import { projectApps, projectLabel, projectNewApp } from '../utils/paths'
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

const projects = useProjectsStore()
const route = useRoute()
const router = useRouter()
// The project a template deploys into: ?project= from the New app chooser, else the current project (named on the page).
const target = computed(() => projects.projects.find((x) => x.id === Number(route.query.project)) ?? projects.current)

// Project step: choosing a template asks which project it deploys into (skipped when there is only one).
const picking = ref<AppTemplate | null>(null)
const pickPid = ref<number | null>(null)
function chooseTemplate(t: AppTemplate) {
  const list = projects.projects
  if (list.length <= 1) return go(t, list[0]?.id ?? null)
  picking.value = t
  pickPid.value = target.value?.id ?? list[0]?.id ?? null
  nextTick(() => {
    const sel = document.getElementById('tpl-project') as HTMLSelectElement | null
    sel?.scrollIntoView({ block: 'center' })
    sel?.focus({ preventScroll: true })
  })
}
function cancelPick() {
  const id = picking.value?.id
  picking.value = null
  nextTick(() => (document.querySelector(`[data-use-template="${id}"]`) as HTMLElement | null)?.focus())
}
function go(t: AppTemplate, pid: number | null) {
  router.push({ path: pid !== null ? projectNewApp(pid) : '/apps/new', query: { template: t.id } })
}

onMounted(async () => {
  if (!projects.loaded) await projects.load()
  try {
    templates.value = await listTemplates((t) => (templates.value = t))
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load templates."
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <main id="main" tabindex="-1" class="page-main">
    <nav class="meta mb-2" aria-label="Breadcrumb">
      <RouterLink :to="target ? projectApps(target.id) : projects.appsHome" class="link">Apps</RouterLink><span aria-hidden="true"> / </span>Templates
    </nav>
    <h1 class="page-title">Templates</h1>
    <p class="help mb-3 max-w-3xl">Pick a ready-made app. "Use this template" opens the new-app form prefilled with add-ons, environment variables, health check and volume, and you can change anything before creating. Secrets are listed but not generated: you set their values.</p>

    <p v-if="offNotes.length" class="alert-warn mb-3 max-w-3xl">Off on this instance: {{ offNotes.join(', ') }}. Templates that need them are marked below; templates with an optional volume deploy without it, and data on scratch disk is lost on restart.</p>

    <div v-if="loading" class="text-sm text-zinc-400" aria-busy="true">Loading…</div>
    <div v-else-if="error" role="alert" class="alert-error">{{ error }}</div>
    <p v-else-if="!templates.length" class="meta">No templates are available.</p>
    <table v-else class="tbl tbl-stack">
      <colgroup><col class="w-[14%]"><col><col class="w-[12%]"><col class="w-[22%]"><col class="w-72"></colgroup>
      <thead><tr><th>Template</th><th>Description</th><th>Add-ons</th><th>Image</th><th class="text-right"><span class="sr-only">Actions</span></th></tr></thead>
      <tbody>
        <template v-for="t in templates" :key="t.id">
        <tr class="align-top">
          <td class="font-medium text-white wrap-break-word">{{ t.name }}</td>
          <td class="wrap-break-word text-zinc-300">
            {{ t.description }}
            <p v-if="t.volume && !volumesOn" class="meta">{{ t.volume.required ? 'Needs a persistent volume, which is off here.' : 'Deploys without the optional volume.' }}</p>
            <p v-if="t.available === false" class="meta text-amber-300!">PostgreSQL is not available. Contact your platform administrator.</p>
            <p v-else-if="(t.wants_db && dbOff) || (t.wants_storage && storageOff)" class="meta">Needs an add-on that is off here.</p>
          </td>
          <td class="text-zinc-300">
            <template v-if="!t.wants_db && !t.wants_storage && !t.volume && !t.secrets.length"><span class="meta">None</span></template>
            <span v-if="t.wants_db" class="block">{{ dbEngineLabel(t.db_engine) }}</span>
            <span v-if="t.wants_storage" class="block">Object Storage</span>
            <span v-if="t.volume" class="block">Block Storage{{ t.volume.required ? '' : ' (optional)' }}</span>
            <span v-if="t.secrets.length" class="meta block">{{ t.secrets.length }} secret{{ t.secrets.length === 1 ? '' : 's' }}</span>
          </td>
          <td class="mono"><span class="block truncate" :title="t.image">{{ t.image }}</span></td>
          <td>
            <div class="row-acts flex-wrap gap-x-4 md:flex-nowrap md:whitespace-nowrap [&>*]:ml-0!">
              <button type="button" class="row-act" :class="t.available !== false && 'text-white'" :disabled="t.available === false" :data-use-template="t.id" @click="chooseTemplate(t)">Use this template</button>
              <a v-if="t.docs_url" :href="t.docs_url" target="_blank" rel="noopener noreferrer" class="row-act">Setup guide</a>
              <a v-if="t.upstream_docs_url" :href="t.upstream_docs_url" target="_blank" rel="noopener noreferrer" class="row-act" :title="`${docsName(t)} documentation`">Docs<span class="sr-only"> ({{ docsName(t) }})</span></a>
            </div>
          </td>
        </tr>
        <tr v-if="picking?.id === t.id" class="tpl-step">
          <td colspan="5" @keydown.esc="cancelPick">
            <p class="section-title">Step 1 of 2: choose a project for {{ t.name }}</p>
            <p class="help">The app is created in this project, and everyone in it can manage the app. Step 2 is the new-app form.</p>
            <div class="mt-2 flex flex-wrap items-center gap-3">
              <label for="tpl-project" class="sr-only">Project</label>
              <select id="tpl-project" v-model="pickPid" class="input max-w-xs">
                <option v-for="p in projects.projects" :key="p.id" :value="p.id">{{ projectLabel(p) }}</option>
              </select>
              <button type="button" class="btn-primary" :disabled="pickPid === null" @click="go(t, pickPid)">Continue</button>
              <button type="button" class="btn-ghost" @click="cancelPick">Cancel</button>
            </div>
          </td>
        </tr>
        </template>
      </tbody>
    </table>
  </main>
</template>
