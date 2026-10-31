<script setup lang="ts">
import { computed, nextTick, reactive, ref } from 'vue'
import ConfirmDialog from './ConfirmDialog.vue'
import { unsuspendApp, unsuspendProject, unsuspendUser } from '../api/admin'
import { ApiError } from '../api/client'
import { useToastStore } from '../stores/toast'
import type { Suspension } from '../types'
import { fullDate, humanDate } from '../utils/format'

// Admin > Suspensions: everything stopped on purpose. Apps stopped because their project or user is suspended
// nest under that cause (collapsed) and have no button of their own.
const props = defineProps<{ items: Suspension[]; loaded: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const toast = useToastStore()

const keyOf = (type: string, id: number) => `${type}:${id}`
const hasCause = (r: Suspension) => r.type === 'app' && (r.via === 'project' || r.via === 'user') && !!r.cause_type && r.cause_id !== null
const causeKey = (r: Suspension) => keyOf(r.cause_type!, r.cause_id!)
const heads = computed(() => props.items.filter((r) => !hasCause(r)))
const headKeys = computed(() => new Set(heads.value.map((r) => keyOf(r.type, r.id))))
// A child nests only if its cause row is in the list; otherwise it stays a top-level row (with a link, no button).
const isNested = (r: Suspension) => hasCause(r) && headKeys.value.has(causeKey(r))
const rows = computed(() => props.items.filter((r) => !isNested(r)))
const kidsOf = computed(() => {
  const m = new Map<string, Suspension[]>()
  for (const r of props.items) if (isNested(r)) m.set(causeKey(r), [...(m.get(causeKey(r)) ?? []), r])
  return m
})
const kids = (r: Suspension) => kidsOf.value.get(keyOf(r.type, r.id)) ?? []
const open = reactive<Record<string, boolean>>({})
const rowId = (r: Suspension) => `sus-${r.type}-${r.id}`
const typeLabel = { user: 'User', project: 'Project', app: 'App' } as const
const causeWord = (r: Suspension) => (r.cause_type === 'user' ? 'user' : 'project')
const nameTo = (r: Suspension) => (r.type === 'project' ? `/projects/${r.id}` : r.type === 'app' ? `/apps/${r.id}` : { path: '/admin', query: { q: r.name } })

async function jumpToCause(c: Suspension) {
  await nextTick()
  const el = document.getElementById(`sus-${c.cause_type}-${c.cause_id}`)
  el?.scrollIntoView({ block: 'center' })
  el?.querySelector<HTMLElement>('button[data-unsuspend]')?.focus()
}

const target = ref<Suspension | null>(null)
const busy = ref(false)
const err = ref('')
const msgs = computed(() => {
  const t = target.value
  if (!t) return { title: '', message: '', label: '' }
  const n = t.app_count ?? 0
  const apps = `${n} ${n === 1 ? 'app' : 'apps'}`
  if (t.type === 'project')
    return { title: `Unsuspend ${t.name}`, message: `${apps} start again. Apps suspended on their own stay suspended. Kaisen builds that were stopped do not resume; members can start new ones.`, label: 'Unsuspend project' }
  if (t.type === 'user')
    return { title: `Unsuspend ${t.name}`, message: `They can sign in again. Their personal project and its ${apps} start again. Apps suspended on their own stay suspended.`, label: 'Unsuspend user' }
  return { title: `Unsuspend ${t.name}`, message: 'The app starts again and its owner can edit and restart it.', label: 'Unsuspend app' }
})
async function confirmUnsuspend() {
  const t = target.value
  if (!t) return
  busy.value = true
  err.value = ''
  try {
    if (t.type === 'project') await unsuspendProject(t.id)
    else if (t.type === 'user') await unsuspendUser(t.id)
    else await unsuspendApp(t.id)
    toast.success(`${t.name} unsuspended.`)
    target.value = null
    emit('changed')
  } catch (e) {
    target.value = null
    err.value = `Couldn't unsuspend ${t.name}: ${e instanceof ApiError ? e.message : 'try again'}`
  } finally {
    busy.value = false
  }
}

const grid = 'md:grid md:grid-cols-[4.5rem_minmax(0,1.1fr)_minmax(0,1fr)_minmax(0,1.5fr)_9rem_8rem] md:items-start md:gap-3'
const rowBase = 'row-pad flex flex-col gap-1 text-sm text-zinc-200'
</script>

<template>
  <div class="space-y-3">
    <p class="help">Everything that is stopped on purpose. Unsuspending restores only what that suspension stopped.</p>
    <p v-if="err" role="alert" class="alert-error">{{ err }}</p>
    <p v-if="!loaded" class="meta" aria-busy="true">Loading suspensions…</p>
    <p v-else-if="rows.length === 0" class="card card-pad meta">Nothing is suspended. To suspend a project, use the Projects tab; for a user, the Users tab.</p>
    <div v-else class="card overflow-hidden">
      <div :class="[grid, 'table-th row-pad hidden border-b border-pico-border']">
        <span>Type</span><span>Name</span><span>Owner / project</span><span>Reason</span><span>Suspended</span><span class="text-right">Action</span>
      </div>
      <ul class="divide-y divide-pico-border">
        <template v-for="r in rows" :key="rowId(r)">
          <li :id="rowId(r)">
            <div :class="[rowBase, grid]">
              <div class="flex flex-wrap items-center gap-1.5 md:block">
                <span class="badge-info">{{ typeLabel[r.type] }}</span>
                <RouterLink :to="nameTo(r)" class="link min-w-0 break-all md:hidden">{{ r.name }}</RouterLink>
              </div>
              <div class="min-w-0">
                <RouterLink :to="nameTo(r)" class="link hidden break-all md:inline">{{ r.name }}</RouterLink>
                <div v-if="kids(r).length">
                  <button type="button" class="link text-xs" :aria-expanded="!!open[rowId(r)]" @click="open[rowId(r)] = !open[rowId(r)]">
                    <span aria-hidden="true">{{ open[rowId(r)] ? '▾' : '▸' }} </span>{{ kids(r).length }} {{ kids(r).length === 1 ? 'app' : 'apps' }} stopped
                  </button>
                </div>
              </div>
              <div class="meta wrap-break-word">
                <template v-if="r.type === 'project'">owner {{ r.owner_email ?? 'none' }}</template>
                <template v-else-if="r.type === 'user'">personal project included</template>
                <template v-else>{{ r.project_name ?? 'No project' }}<template v-if="r.owner_email"> · {{ r.owner_email }}</template></template>
              </div>
              <div class="wrap-break-word">
                <template v-if="r.reason">{{ r.reason }}</template>
                <span v-else-if="hasCause(r)" class="badge-warn">Via {{ causeWord(r) }}</span>
                <span v-else class="text-zinc-400">No reason recorded <span class="meta">(suspended before reasons existed)</span></span>
              </div>
              <div>
                <span v-if="r.suspended_at" :title="fullDate(r.suspended_at)">{{ humanDate(r.suspended_at) }}</span>
                <span v-else class="text-zinc-400">Unknown</span>
                <div v-if="r.suspended_by_email" class="meta break-all">by {{ r.suspended_by_email }}</div>
              </div>
              <div class="md:text-right">
                <RouterLink v-if="hasCause(r)" :to="{ path: '/admin', query: r.cause_type === 'user' ? {} : { tab: 'projects' } }" class="link text-xs">Unsuspend the {{ causeWord(r) }}</RouterLink>
                <button v-else type="button" class="btn-secondary btn-sm" data-unsuspend :aria-label="`Unsuspend ${r.name}`" @click="target = r">Unsuspend</button>
              </div>
            </div>
          </li>
          <li v-for="c in open[rowId(r)] ? kids(r) : []" :key="rowId(c)" class="bg-pico-bg/40">
            <div :class="[rowBase, grid]">
              <div class="flex flex-wrap items-center gap-1.5 md:block"><span class="badge-neutral">App</span></div>
              <div class="flex min-w-0 flex-wrap items-center gap-1.5">
                <RouterLink :to="`/apps/${c.id}`" class="link break-all">{{ c.name }}</RouterLink>
                <span class="badge-warn md:hidden">Via {{ causeWord(c) }}</span>
              </div>
              <div class="meta hidden break-all md:block">{{ c.project_name ?? '' }}</div>
              <div class="hidden md:block"><span class="badge-warn">Via {{ causeWord(c) }}</span> <span class="meta">same reason</span></div>
              <div class="meta hidden md:block">with the {{ causeWord(c) }}</div>
              <div class="md:text-right">
                <button type="button" class="link text-xs" @click="jumpToCause(c)">Unsuspend the {{ causeWord(c) }}</button>
              </div>
            </div>
          </li>
        </template>
      </ul>
    </div>

    <ConfirmDialog
      :open="target !== null"
      :title="msgs.title"
      :message="msgs.message"
      :confirm-label="msgs.label"
      busy-label="Unsuspending…"
      :busy="busy"
      @confirm="confirmUnsuspend"
      @cancel="target = null"
    />
  </div>
</template>
