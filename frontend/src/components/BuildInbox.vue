<script setup lang="ts">
import { fmtUsd } from '../composables/kaisen'
import type { KaisenInboxItem } from '../types'
import { projectKaisen } from '../utils/paths'

// One open notice (spend cap, refusal, error) shown inline in the thread. Questions are answered from the composer.
defineProps<{ item: KaisenInboxItem; canRun: boolean; spendCap: number; projectId: number }>()
const emit = defineEmits<{ resume: []; dismiss: [id: number] }>()
</script>

<template>
  <p v-if="item.kind === 'budget'" class="alert-warn">
    Kaisen stopped at the spend cap ({{ fmtUsd(spendCap) }}). Raise the cap in <RouterLink :to="projectKaisen(projectId, '/settings')" class="link">Kaisen settings</RouterLink> or <button type="button" class="link" :disabled="!canRun" @click="emit('resume')">Resume</button>.
  </p>
  <p v-else class="alert-error flex items-start justify-between gap-3 whitespace-pre-wrap" role="alert">
    <span class="min-w-0 [overflow-wrap:anywhere]">{{ item.text }}</span>
    <button type="button" class="row-act shrink-0" aria-label="Dismiss alert" @click="emit('dismiss', item.id)">Dismiss</button>
  </p>
</template>
