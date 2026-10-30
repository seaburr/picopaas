<script setup lang="ts">
import { ref } from 'vue'
import { X } from 'lucide-vue-next'
import IconButton from './IconButton.vue'
import type { KaisenInboxItem } from '../types'

const props = defineProps<{ items: KaisenInboxItem[]; canRun: boolean; busyId: number | null; spendCap: number }>()
const emit = defineEmits<{ answer: [id: number, text: string]; resume: []; dismiss: [id: number] }>()
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
      <form class="mt-2 flex flex-col gap-2 sm:flex-row" @submit.prevent="send(it.id)">
        <textarea v-model="answers[it.id]" class="input" rows="2" aria-label="Your answer" :disabled="!canRun || busyId === it.id" />
        <button type="submit" class="btn-primary shrink-0 self-start" :disabled="!canRun || busyId === it.id || !(answers[it.id] ?? '').trim()">{{ busyId === it.id ? 'Sending…' : 'Send answer' }}</button>
      </form>
    </section>
    <p v-else-if="it.kind === 'question' && it.status === 'answered'" class="meta">Answered. Kaisen is continuing.</p>
    <p v-else-if="it.kind === 'budget' && it.status === 'open'" class="alert-warn flex flex-wrap items-center justify-between gap-2">
      <span>Kaisen stopped at the spend cap (${{ spendCap }}). Raise the cap in <RouterLink to="/kaisen/settings" class="link">Kaisen settings</RouterLink> or <button type="button" class="link" :disabled="!canRun" @click="emit('resume')">Resume</button>.</span>
    </p>
    <p v-else-if="it.status === 'open' && ['refusal', 'error', 'push', 'deploy'].includes(it.kind)" :class="['flex items-start justify-between gap-2 whitespace-pre-wrap', it.kind === 'push' && !/fail|error/i.test(it.text) ? 'alert-success' : 'alert-error']" role="alert">
      <span>{{ it.text }}</span>
      <IconButton :icon="X" sm label="Dismiss alert" @click="emit('dismiss', it.id)" />
    </p>
  </template>
</template>
