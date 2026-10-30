<script setup lang="ts">
import { computed } from 'vue'

// Status is never colour-only: every state has a text label plus a distinct glyph.
// Labels: pending -> Queued, error -> Failed (see UX copy decisions).
const props = defineProps<{ status: string; kind?: 'app' | 'build' }>()

interface Look { label: string; cls: string; glyph: 'dot' | 'pulse' | 'clock' | 'lock' | 'cross' | 'square' | 'check'; tip: string }
const looks: Record<string, Look> = {
  running: { label: 'Running', cls: 'badge-ok', glyph: 'dot', tip: 'The app is up and serving traffic.' },
  deploying: { label: 'Deploying', cls: 'badge-warn', glyph: 'pulse', tip: 'Starting your app. Usually under a minute.' },
  pending: { label: 'Queued', cls: 'badge-neutral', glyph: 'clock', tip: 'Waiting to be deployed.' },
  retrying: { label: 'Retrying', cls: 'badge-warn', glyph: 'pulse', tip: 'The last attempt failed. Retrying automatically.' },
  error: { label: 'Failed', cls: 'badge-error', glyph: 'cross', tip: 'The app could not be started.' },
  suspended: { label: 'Suspended', cls: 'badge-warn', glyph: 'lock', tip: 'Paused by an administrator.' },
  stopped: { label: 'Stopped', cls: 'badge-neutral', glyph: 'square', tip: 'The app is stopped.' },
  active: { label: 'Active', cls: 'badge-ok', glyph: 'dot', tip: '' },
  revoked: { label: 'Revoked', cls: 'badge-neutral', glyph: 'square', tip: '' },
}
// Kaisen build statuses (kind="build"): "running" would clash with an app's Running.
const buildLooks: Record<string, Look> = {
  idle: { label: 'Not built', cls: 'badge-neutral', glyph: 'square', tip: 'No run yet.' },
  queued: { label: 'Queued', cls: 'badge-neutral', glyph: 'clock', tip: 'Waiting for the builder.' },
  running: { label: 'Building', cls: 'badge-warn', glyph: 'pulse', tip: 'Kaisen is writing code.' },
  waiting: { label: 'Needs you', cls: 'badge-info', glyph: 'dot', tip: 'Kaisen asked a question.' },
  done: { label: 'Ready', cls: 'badge-ok', glyph: 'dot', tip: 'Last run finished.' },
  error: { label: 'Failed', cls: 'badge-error', glyph: 'cross', tip: 'The last run stopped with an error.' },
  stopped: { label: 'Stopped', cls: 'badge-neutral', glyph: 'square', tip: 'Stopped at a spend cap, a refusal or a loop guard.' },
}
const look = computed<Look>(
  () => (props.kind === 'build' ? buildLooks : looks)[props.status] ?? { label: props.status ? props.status[0].toUpperCase() + props.status.slice(1) : 'Unknown', cls: 'badge-neutral', glyph: 'dot', tip: '' },
)
</script>

<template>
  <span :class="look.cls" :title="look.tip || undefined">
    <svg v-if="look.glyph === 'clock'" class="h-3 w-3" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="6" cy="6" r="4.5" /><path d="M6 3.5V6l1.8 1.1" /></svg>
    <svg v-else-if="look.glyph === 'lock'" class="h-3 w-3" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="2.5" y="5.5" width="7" height="5" rx="1" /><path d="M4 5.5V4a2 2 0 014 0v1.5" /></svg>
    <svg v-else-if="look.glyph === 'cross'" class="h-3 w-3" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6" /></svg>
    <svg v-else-if="look.glyph === 'square'" class="h-3 w-3" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true"><rect x="3" y="3" width="6" height="6" rx="1" /></svg>
    <span v-else :class="['inline-block h-1.5 w-1.5 rounded-full bg-current', look.glyph === 'pulse' ? 'motion-safe:animate-pulse' : '']" aria-hidden="true" />
    {{ look.label }}
  </span>
</template>
