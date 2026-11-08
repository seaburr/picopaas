<script setup lang="ts">
import { computed } from 'vue'
import { isBusyStatus, statusLook } from '../composables/statusTone'

// Dot + text in the state colour, no pill. Status is never colour-only: the dot differs by shape per tone
// (ok solid circle, warn/off hollow circle, err square) and the label is always present.
// Labels: pending -> Queued, error -> Failed (see UX copy decisions).
const props = defineProps<{ status: string; kind?: 'app' | 'build' }>()

const TIPS: Record<string, string> = {
  running: 'The app is up and serving traffic.',
  updating: 'The app is up. A new version is being rolled out and the current one stays live.',
  deploying: 'Starting your app. Usually under a minute.',
  pending: 'Waiting to be deployed.',
  retrying: 'The last attempt failed. Retrying automatically.',
  error: 'The app could not be started.',
  suspended: 'Paused by an administrator.',
  stopped: 'The app is stopped.',
}
const BUILD_TIPS: Record<string, string> = {
  idle: 'No run yet.',
  queued: 'Waiting for the builder.',
  running: 'Kaisen is writing code.',
  waiting: 'Kaisen asked a question.',
  done: 'Last run finished.',
  error: 'The last run stopped with an error.',
  stopped: 'Stopped at a spend cap, a refusal or a loop guard.',
}
const look = computed(() => statusLook(props.status, props.kind))
const tip = computed(() => (props.kind === 'build' ? BUILD_TIPS : TIPS)[props.status])
const busy = computed(() => isBusyStatus(props.status, props.kind))
</script>

<template>
  <span :class="['st', `tone-${look.tone}`]" :title="tip || undefined">
    <span :class="['dot', `dot-${look.tone}`, busy ? 'motion-safe:animate-pulse' : '']" aria-hidden="true" />
    {{ look.label }}
  </span>
</template>
