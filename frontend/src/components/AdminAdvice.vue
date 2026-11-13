<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { listAdminAdvice } from '../api/admin'
import { ApiError } from '../api/client'
import { SEVERITY } from '../composables/advice'
import type { AdminAdviceFinding } from '../types'

// Admin > Advice: every open health finding across apps, as returned (critical first). Read-only.
const rows = ref<AdminAdviceFinding[]>([])
const loading = ref(true)
const error = ref('')
onMounted(async () => {
  try {
    rows.value = (await listAdminAdvice()).findings ?? []
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load advice."
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="space-y-3">
    <p class="help">Open health findings across all apps.</p>
    <p v-if="error" role="alert" class="alert-error">{{ error }}</p>
    <p v-else-if="loading" class="meta" role="status" aria-busy="true">Loading advice…</p>
    <p v-else-if="rows.length === 0" class="sec meta py-2">No open advice.</p>
    <div v-else class="overflow-x-auto">
      <table class="tbl tbl-stack md:min-w-[70rem]" aria-label="Open advice">
        <colgroup><col class="w-44" /><col class="w-32" /><col class="w-48" /><col class="w-24" /><col class="w-44" /><col class="w-[26rem]" /></colgroup>
        <thead>
          <tr><th scope="col">App</th><th scope="col">Project</th><th scope="col">Owner</th><th scope="col">Severity</th><th scope="col">Rule</th><th scope="col">Evidence</th></tr>
        </thead>
        <tbody class="text-zinc-200">
          <tr v-for="f in rows" :key="`${f.app_id}-${f.rule}`" :title="f.rule" class="align-top">
            <td class="wrap-break-word"><RouterLink :to="`/apps/${f.app_id}`" class="link mono break-all">{{ f.app_name }}</RouterLink></td>
            <td class="meta wrap-break-word">{{ f.project_name ?? 'No project' }}</td>
            <td class="meta break-all">{{ f.owner_email ?? '' }}</td>
            <td><span :class="['st', `tone-${SEVERITY[f.severity].tone}`]"><span :class="['dot', `dot-${SEVERITY[f.severity].tone}`]" aria-hidden="true" />{{ SEVERITY[f.severity].word }}</span></td>
            <td class="wrap-break-word">{{ f.title }}</td>
            <td class="wrap-break-word text-zinc-300">{{ f.evidence }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
