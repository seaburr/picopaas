<script setup lang="ts">
import { computed } from 'vue'
import { failureInfo, retryText, useNow } from '../composables/appUi'
import type { App } from '../types'

// Human status line + recovery actions for an app. `compact` is the list-row variant.
const props = defineProps<{ app: App; compact?: boolean; restarting?: boolean }>()
defineEmits<{ restart: [] }>()
const now = useNow()

const failure = computed(() => (props.app.status === 'error' ? failureInfo(props.app) : null))
const retry = computed(() => (props.app.status === 'error' && props.app.retry_at ? retryText(props.app.retry_at, now.value) : ''))
const busy = computed(() => props.app.status === 'deploying')
</script>

<template>
  <div v-if="app.status === 'suspended'" class="text-sm text-amber-300">
    Suspended by an administrator. Contact the administrator to restore it.
  </div>

  <div v-else-if="app.status === 'pending' || app.status === 'deploying'" class="text-sm">
    <p class="text-zinc-300">
      {{ busy ? 'Starting your app…' : 'Queued. Deploy starts shortly.' }}
      <span v-if="!compact" class="text-zinc-400">Usually about a minute.</span>
    </p>
    <div v-if="busy" class="indet mt-2 h-1 w-full max-w-xs overflow-hidden rounded bg-pico-card" role="progressbar" aria-label="Deploying" />
  </div>

  <div v-else-if="failure" class="text-sm">
    <p class="break-words text-zinc-200">{{ failure.text }}</p>
    <p v-if="retry" class="meta mt-0.5">{{ retry }}</p>
    <p v-else class="meta mt-0.5">Won't retry until you change something.</p>
    <div class="mt-2 flex flex-wrap gap-2">
      <RouterLink v-if="failure.actions.includes('edit')" :to="`/apps/${app.id}/edit`" class="btn-secondary btn-sm">
        {{ failure.reason === 'image_pull' || failure.reason === 'root_image' ? 'Edit image' : 'Edit app' }}
      </RouterLink>
      <RouterLink v-if="failure.actions.includes('logs')" :to="`/apps/${app.id}#logs`" class="btn-secondary btn-sm">
        View logs
      </RouterLink>
      <button v-if="failure.actions.includes('restart')" type="button" class="btn-secondary btn-sm" :disabled="restarting" @click="$emit('restart')">
        {{ restarting ? 'Restarting…' : 'Restart' }}
      </button>
    </div>
  </div>

  <p v-else-if="!compact && app.status_detail" class="break-words text-sm text-zinc-300">{{ app.status_detail }}</p>
</template>

<style scoped>
.indet {
  position: relative;
}
.indet::after {
  content: '';
  position: absolute;
  inset: 0 auto 0 0;
  width: 35%;
  background: #7aa0d4;
  border-radius: 2px;
  animation: indet 1.4s ease-in-out infinite;
}
@keyframes indet {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(300%); }
}
@media (prefers-reduced-motion: reduce) {
  .indet::after { animation: none; width: 100%; opacity: 0.5; }
}
</style>
