<script setup lang="ts">
import type { KaisenInboxItem } from '../types'

// One open notice (spend cap, refusal, error) shown inline in the thread. Questions are answered from the composer.
defineProps<{ item: KaisenInboxItem; canRun: boolean; spendCap: number }>()
const emit = defineEmits<{ resume: []; dismiss: [id: number] }>()
</script>

<template>
  <p v-if="item.kind === 'budget'" class="alert-warn">
    Kaisen stopped at the spend cap (${{ spendCap }}). Raise the cap in <RouterLink to="/kaisen/settings" class="link">Kaisen settings</RouterLink> or <button type="button" class="link" :disabled="!canRun" @click="emit('resume')">Resume</button>.
  </p>
  <p v-else class="alert-error flex items-start justify-between gap-3 whitespace-pre-wrap" role="alert">
    <span class="min-w-0 [overflow-wrap:anywhere]">{{ item.text }}</span>
    <button type="button" class="row-act shrink-0" aria-label="Dismiss alert" @click="emit('dismiss', item.id)">Dismiss</button>
  </p>
</template>
