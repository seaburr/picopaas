<script setup lang="ts">
import { computed } from 'vue'
import { relTime } from '../composables/appUi'
import { stampTitle } from '../utils/format'
import type { KaisenInboxItem } from '../types'

const props = defineProps<{ items: KaisenInboxItem[]; now: number; phase?: 'updating' | 'starting' | 'live' | 'other' }>()

// The newest deploy record, whatever its status: it stays on the page as a record, there is nothing to dismiss.
const item = computed(() => props.items.filter((i) => i.kind === 'deploy').reduce<KaisenInboxItem | null>((a, i) => (!a || i.id > a.id ? i : a), null))
const OK = 'Preview deployed: '
const UNHEALTHY = 'Preview deployed but not healthy yet: '
const look = computed(() => {
  // The record says the deploy went out; the app may not be running the new version yet. Say so until it is.
  if (props.phase === 'updating') return { label: 'Running · updating', tone: 'ok', quiet: true }
  if (props.phase === 'starting') return { label: 'Starting', tone: 'off', quiet: true }
  const t = item.value?.text ?? ''
  if (t.startsWith(OK)) return { label: 'Deployed', tone: 'ok', quiet: true }
  if (t.startsWith(UNHEALTHY)) return { label: 'Not healthy yet', tone: 'warn', quiet: false }
  if (t.startsWith('Deployment started')) return { label: 'Started', tone: 'off', quiet: true }
  return { label: 'Failed', tone: 'err', quiet: false }
})
const http = computed(() => /^http: (.+)$/m.exec(item.value?.text ?? '')?.[1] ?? '')
const output = computed(() => (item.value?.text ?? '').replace(OK, '').replace(UNHEALTHY, ''))
</script>

<template>
  <div v-if="item" class="mt-3 border-t border-pico-border pt-2" aria-labelledby="h-last-deploy">
    <div class="flex flex-wrap items-center gap-2">
      <h3 id="h-last-deploy" class="text-xs font-medium text-zinc-200">Last deployment</h3>
      <span :class="['st', `tone-${look.tone}`]"><span :class="['dot', `dot-${look.tone}`]" aria-hidden="true" />{{ look.label }}</span>
      <span class="meta mono" :title="stampTitle(item.created_at)">{{ relTime(item.created_at, now) }}<template v-if="http"> · HTTP {{ http }}</template></span>
    </div>
    <!-- Open by default only when something needs reading. Keyed so a new deployment gets its own default. -->
    <details :key="item.id" class="disc mt-1" :open="!look.quiet">
      <summary class="text-sm">Deployment output</summary>
      <pre class="mono mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-pico-bg p-3 text-xs text-zinc-200" tabindex="0" aria-label="Deployment output">{{ output }}</pre>
    </details>
  </div>
</template>
