<script setup lang="ts">
import { ref } from 'vue'
import { X } from 'lucide-vue-next'
import AttachComposer from './AttachComposer.vue'
import IconButton from './IconButton.vue'
import type { KaisenInboxItem, KaisenUploadsSummary } from '../types'

const props = defineProps<{ items: KaisenInboxItem[]; canRun: boolean; busyId: number | null; spendCap: number; buildId: number; summary?: KaisenUploadsSummary }>()
const emit = defineEmits<{ answer: [id: number, text: string]; resume: []; dismiss: [id: number]; 'open-files': []; changed: [] }>()
const answers = ref<Record<number, string>>({})
function send(id: number) {
  const t = (answers.value[id] ?? '').trim()
  if (t) emit('answer', id, t)
}
void props
</script>

<template>
  <template v-for="it in items" :key="it.id">
    <section v-if="it.kind === 'question' && it.status === 'open'" class="alert-info" role="status" aria-labelledby="h-question">
      <h2 id="h-question" class="section-title mb-1">Needs your answer</h2>
      <p class="whitespace-pre-wrap text-sm">Kaisen asks: {{ it.text }}</p>
      <div class="mt-2">
        <AttachComposer
          :model-value="answers[it.id] ?? ''" :build-id="buildId" :summary="summary" label="Your answer" placeholder="Your answer. Attach files if it helps."
          submit-label="Send answer" :busy="busyId === it.id" :locked="canRun ? '' : 'You can not answer for this build.'" :can-attach="canRun" :rows="2"
          @update:model-value="answers[it.id] = $event" @submit="send(it.id)" @open-files="emit('open-files')" @changed="emit('changed')"
        />
      </div>
    </section>
    <p v-else-if="it.kind === 'question' && it.status === 'answered'" class="meta">Answered. Kaisen is continuing.</p>
    <p v-else-if="it.kind === 'budget' && it.status === 'open'" class="alert-warn flex flex-wrap items-center justify-between gap-2">
      <span>Kaisen stopped at the spend cap (${{ spendCap }}). Raise the cap in <RouterLink to="/kaisen/settings" class="link">Kaisen settings</RouterLink> or <button type="button" class="link" :disabled="!canRun" @click="emit('resume')">Resume</button>.</span>
    </p>
    <p v-else-if="it.status === 'open' && ['refusal', 'error', 'deploy'].includes(it.kind)" :class="['flex items-start justify-between gap-2 whitespace-pre-wrap', (it.kind === 'push' && !/fail|error/i.test(it.text)) || (it.kind === 'deploy' && it.text.startsWith('Preview deployed: ')) ? 'alert-success' : 'alert-error']" role="alert">
      <span>{{ it.text }}</span>
      <IconButton :icon="X" sm label="Dismiss alert" @click="emit('dismiss', it.id)" />
    </p>
  </template>
</template>
