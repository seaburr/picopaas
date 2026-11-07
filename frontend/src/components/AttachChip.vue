<script setup lang="ts">
import { fmtSize } from '../composables/kaisen'
import type { Chip } from '../composables/useAttachments'

defineProps<{ chip: Chip }>()
const emit = defineEmits<{ remove: []; retry: []; note: [v: string]; save: []; 'open-files': [] }>()
</script>

<template>
  <li class="rounded-md border px-2 py-1.5" :class="chip.status === 'error' ? 'border-danger/60' : 'border-pico-border'">
    <div class="flex items-start gap-2">
      <img v-if="chip.thumb" :src="chip.thumb" alt="" class="h-8 w-8 shrink-0 rounded-sm bg-pico-bg object-cover" />
      <span v-else class="mono flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-pico-bg text-zinc-300" aria-hidden="true">{{ (chip.name.split('.').pop() ?? '').slice(0, 4).toUpperCase() }}</span>
      <div class="min-w-0 flex-1">
        <p class="break-all text-sm font-medium text-zinc-100">{{ chip.name }}</p>
        <p class="meta">
          <template v-if="chip.dims">{{ chip.dims }} · </template>{{ fmtSize(chip.size) }}
          <span v-if="chip.status === 'uploading'" class="ml-1 motion-safe:animate-pulse">Uploading…</span>
          <span v-else-if="chip.status === 'ready'" class="ml-1 text-status-running">Uploaded</span>
          <span v-else-if="chip.status === 'held'" class="ml-1">Will upload</span>
        </p>
        <p v-if="chip.status !== 'error'" class="meta">
          {{ chip.kind === 'asset' ? 'Use in the site' : 'Reference only' }} (set from the file type)
          <template v-for="n in chip.notes" :key="n"> · {{ n }}</template>
        </p>
      </div>
      <button type="button" class="row-act shrink-0" :aria-label="chip.status === 'uploading' ? `Cancel ${chip.name}` : `Remove ${chip.name}`" @click="emit('remove')">{{ chip.status === 'uploading' ? 'Cancel' : 'Remove' }}</button>
    </div>
    <div v-if="chip.status === 'ready' || chip.status === 'held'" class="mt-2">
      <label :for="`chip-note-${chip.key}`" class="sr-only">Note for {{ chip.name }}</label>
      <input :id="`chip-note-${chip.key}`" class="input" :value="chip.note" maxlength="200" placeholder="Note (optional): what it is, used as alt text" @blur="emit('save')" @keydown.enter.prevent="emit('save')" @input="emit('note', ($event.target as HTMLInputElement).value)" />
    </div>
    <p v-if="chip.status === 'error'" class="field-error mt-1" role="alert">
      {{ chip.error }}
      <button v-if="chip.quota" type="button" class="link" @click="emit('open-files')">Open Files</button>
      <button v-if="chip.file && chip.retryable" type="button" class="link ml-1" @click="emit('retry')">Try again</button>
    </p>
  </li>
</template>
