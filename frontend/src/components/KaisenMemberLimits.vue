<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { RotateCw, Save } from 'lucide-vue-next'
import IconButton from './IconButton.vue'
import { ApiError } from '../api/client'
import { getKaisenMembers, saveKaisenMember } from '../api/kaisen'
import { fmtUsd } from '../composables/kaisen'
import { useToastStore } from '../stores/toast'
import type { KaisenMembers } from '../types'

const props = defineProps<{ projectId: number }>()
const toast = useToastStore()

const data = ref<KaisenMembers | null>(null)
const loading = ref(true)
const error = ref('')
const rowErr = reactive<Record<number, string>>({})
const savingId = ref<number | null>(null)
// Editable text per member; blank = no limit.
const edit = reactive<Record<number, { day: string; build: string }>>({})

const canEdit = computed(() => !!data.value?.can_edit)
const num = (v: number | null) => (v === null || v === undefined ? '' : String(v))

function fill(d: KaisenMembers) {
  data.value = d
  for (const m of d.members) edit[m.user_id] = { day: num(m.cap_day), build: num(m.cap_build) }
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    fill(await getKaisenMembers(props.projectId))
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load member limits."
  } finally {
    loading.value = false
  }
}
watch(() => props.projectId, load, { immediate: true })

const toCap = (s: string) => (s.trim() === '' ? null : Number(s))
const rowDirty = (id: number) => {
  const m = data.value?.members.find((x) => x.user_id === id)
  const e = edit[id]
  return !!m && !!e && (e.day.trim() !== num(m.cap_day) || e.build.trim() !== num(m.cap_build))
}
async function save(id: number) {
  const e = edit[id]
  if (!e) return
  delete rowErr[id]
  const day = toCap(e.day)
  const build = toCap(e.build)
  if ((day !== null && !(day >= 0)) || (build !== null && !(build >= 0))) {
    rowErr[id] = 'Enter an amount in USD, or leave blank for no limit.'
    return
  }
  savingId.value = id
  try {
    await saveKaisenMember(props.projectId, id, { cap_day: day, cap_build: build })
    fill(await getKaisenMembers(props.projectId))
    toast.success('Member limits saved.')
  } catch (err) {
    rowErr[id] = err instanceof ApiError ? err.message : "Couldn't save the limits."
  } finally {
    savingId.value = null
  }
}
const cap = (v: number | null) => (v === null ? 'No limit' : fmtUsd(v))
const cols = computed(() => (canEdit.value
  ? 'sm:grid-cols-[minmax(0,2fr)_5rem_6rem_7rem_7rem_2.5rem]'
  : 'sm:grid-cols-[minmax(0,2fr)_5rem_6rem_7rem_7rem]'))
</script>

<template>
  <section class="card card-pad min-w-0 space-y-3 lg:col-span-2" aria-labelledby="ks-members">
    <h2 id="ks-members" class="section-title">Member limits (USD)</h2>
    <p class="help">Caps for each person's own spend, on top of the project limits above. Blank means no limit. Resets at 00:00 UTC; counts all builds in this project.</p>

    <div v-if="error" role="alert" class="alert-error flex flex-wrap items-center justify-between gap-2">
      <span>{{ error }}</span>
      <IconButton :icon="RotateCw" label="Retry" @click="load" />
    </div>
    <div v-else-if="loading" class="h-16 animate-pulse rounded-sm bg-pico-card" aria-busy="true" aria-label="Loading member limits" />
    <p v-else-if="!data || data.members.length === 0" class="meta">No members yet.</p>
    <div v-else role="table" aria-label="Member spend limits" class="text-sm">
      <div role="row" class="meta hidden gap-3 pb-1 sm:grid" :class="cols">
        <span role="columnheader">Member</span>
        <span role="columnheader">Role</span>
        <span role="columnheader">Spent today</span>
        <span role="columnheader">Daily limit</span>
        <span role="columnheader">Per-build limit</span>
        <span v-if="canEdit" role="columnheader" class="sr-only">Save</span>
      </div>
      <div v-for="m in data.members" :key="m.user_id" role="row" class="grid items-center gap-x-3 gap-y-2 border-t border-pico-line py-2" :class="cols">
        <div role="cell" class="min-w-0 break-all text-zinc-200">{{ m.email }}</div>
        <div role="cell" class="text-zinc-300"><span class="meta sm:hidden">Role: </span>{{ m.role }}</div>
        <div role="cell" class="mono text-zinc-300"><span class="meta sm:hidden">Spent today: </span>{{ fmtUsd(m.spend_today_usd) }}</div>
        <template v-if="canEdit && edit[m.user_id]">
          <div role="cell"><label class="meta sm:hidden" :for="`day-${m.user_id}`">Daily limit</label><input :id="`day-${m.user_id}`" v-model="edit[m.user_id]!.day" type="number" step="0.01" min="0" inputmode="decimal" class="input w-full sm:w-24" placeholder="No limit" :aria-label="`Daily limit for ${m.email}`" /></div>
          <div role="cell"><label class="meta sm:hidden" :for="`bld-${m.user_id}`">Per-build limit</label><input :id="`bld-${m.user_id}`" v-model="edit[m.user_id]!.build" type="number" step="0.01" min="0" inputmode="decimal" class="input w-full sm:w-24" placeholder="No limit" :aria-label="`Per-build limit for ${m.email}`" /></div>
          <div role="cell"><IconButton :icon="Save" :label="`Save limits for ${m.email}`" :busy="savingId === m.user_id" :disabled="!rowDirty(m.user_id)" @click="save(m.user_id)" /></div>
        </template>
        <template v-else>
          <div role="cell" class="mono text-zinc-300"><span class="meta sm:hidden">Daily limit: </span>{{ cap(m.cap_day) }}</div>
          <div role="cell" class="mono text-zinc-300"><span class="meta sm:hidden">Per-build limit: </span>{{ cap(m.cap_build) }}</div>
        </template>
        <p v-if="rowErr[m.user_id]" class="field-error col-span-full" role="alert">{{ rowErr[m.user_id] }}</p>
      </div>
    </div>
    <p v-if="data && !canEdit" class="help">Only project owners can change member limits.</p>
  </section>
</template>
