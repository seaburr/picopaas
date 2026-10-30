<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Check, RotateCw } from 'lucide-vue-next'
import IconButton from './IconButton.vue'
import { listAdminProjects, setProjectAppLimit } from '../api/admin'
import { ApiError } from '../api/client'
import { useToastStore } from '../stores/toast'
import type { AdminProject } from '../types'

// Admin > Projects: shared projects and their app-limit override (blank = platform default).
const toast = useToastStore()
const rows = ref<AdminProject[]>([])
const loading = ref(true)
const error = ref('')
const rowError = reactive<Record<number, string>>({})
const draft = reactive<Record<number, string>>({})

// Rows without an override show the platform default as their effective limit.
const defaultLimit = computed(() => rows.value.find((p) => p.app_limit === null)?.app_limit_effective ?? null)
const orig = (p: AdminProject) => (p.app_limit === null ? '' : String(p.app_limit))

async function load() {
  loading.value = true
  error.value = ''
  try {
    rows.value = (await listAdminProjects()) ?? []
    for (const p of rows.value) draft[p.id] = orig(p)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load projects."
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function save(p: AdminProject) {
  const raw = (draft[p.id] ?? '').trim()
  const value = raw === '' ? null : Number(raw)
  if (value !== null && (!Number.isInteger(value) || value < 0)) {
    rowError[p.id] = 'Enter a whole number, or leave blank for the default.'
    return
  }
  rowError[p.id] = ''
  try {
    const u = await setProjectAppLimit(p.id, value)
    rows.value = rows.value.map((x) => (x.id === p.id ? u : x))
    draft[p.id] = orig(u)
    toast.success(`Updated the limit for ${p.name}.`)
  } catch (e) {
    rowError[p.id] = e instanceof ApiError ? e.message : "Couldn't save the limit."
  }
}
</script>

<template>
  <div>
    <div v-if="error" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
      <span>{{ error }}</span>
      <IconButton :icon="RotateCw" label="Retry" @click="load" />
    </div>
    <p v-else-if="loading" class="meta" aria-busy="true">Loading projects…</p>
    <p v-else-if="rows.length === 0" class="card card-pad meta">No shared projects yet.</p>
    <template v-else>
    <p class="help mb-2">
      App limit is the most apps a project may hold. Leave it blank to use the default from settings ({{ defaultLimit ?? 'unknown' }} apps).
    </p>
    <ul class="card divide-y divide-pico-border">
      <li v-for="p in rows" :key="p.id" class="flex flex-col gap-2 px-3 py-2 sm:flex-row sm:items-center sm:justify-between sm:px-4">
        <div class="min-w-0">
          <div class="truncate font-medium text-white" :title="p.name">{{ p.name }}</div>
          <div class="meta truncate" :title="p.owner_emails.join(', ')">
            {{ p.app_count }} of {{ p.app_limit_effective }} apps · {{ p.member_count }} {{ p.member_count === 1 ? 'member' : 'members' }} · owner {{ p.owner_emails.join(', ') || 'none' }}
          </div>
          <p v-if="rowError[p.id]" role="alert" class="field-error">{{ rowError[p.id] }}</p>
        </div>
        <form class="flex shrink-0 items-center gap-1" @submit.prevent="save(p)">
          <label :for="`pl-${p.id}`" class="meta">App limit <span class="text-zinc-500">(effective {{ p.app_limit_effective }})</span></label>
          <input :id="`pl-${p.id}`" v-model="draft[p.id]" type="text" inputmode="numeric" pattern="[0-9]*" placeholder="blank = default" class="input mono w-40" />
          <button v-if="draft[p.id] !== orig(p)" type="submit" class="btn-icon btn-icon-sm text-accent-light" :aria-label="`Save app limit for ${p.name}`" title="Save limit">
            <Check :size="16" aria-hidden="true" />
          </button>
        </form>
      </li>
    </ul>
    </template>
  </div>
</template>
