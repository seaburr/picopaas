<script setup lang="ts">
import { computed } from 'vue'
import ConfirmDialog from './ConfirmDialog.vue'
import { dbEngineLabel } from '../composables/appUi'
import type { App } from '../types'

const props = defineProps<{ app: App | null; busy?: boolean }>()
defineEmits<{ confirm: []; cancel: [] }>()

const what = computed(() => {
  const a = props.app
  if (!a) return ''
  const parts = [a.want_db ? `its ${dbEngineLabel(a.db_engine)} database` : '', a.want_storage ? 'its blob storage bucket' : ''].filter(Boolean)
  return parts.length ? `, ${parts.join(' and ')} (including all data)` : ''
})
const needsType = computed(() => !!props.app && (props.app.want_db || props.app.want_storage))
</script>

<template>
  <ConfirmDialog
    :open="app !== null"
    title="Delete app"
    :message="`This permanently deletes ${app?.name ?? ''}${what}. This can't be undone.`"
    confirm-label="Delete app"
    busy-label="Deleting…"
    danger
    :busy="busy"
    :require-text="needsType ? app?.name : undefined"
    @confirm="$emit('confirm')"
    @cancel="$emit('cancel')"
  />
</template>
