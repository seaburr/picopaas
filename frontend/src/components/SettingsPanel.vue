<script setup lang="ts">
import { computed, onMounted, reactive, ref, useId } from 'vue'
import { getSettings, putSettings } from '../api/admin'
import { ApiError } from '../api/client'
import { useToastStore } from '../stores/toast'
import { stamp } from '../utils/format'
import type { PlatformSetting, SettingsGroup } from '../types'

const toast = useToastStore()
const uid = useId()
const groups = ref<SettingsGroup[]>([])
const loaded = ref(false)
const loadError = ref('')
const groupError = reactive<Record<string, string>>({})
const busy = ref<string | null>(null)

// Draft per key: text for ints, bool for toggles, unit (MB/GB) for byte-sized settings.
interface Draft { text: string; on: boolean; unit: 'MB' | 'GB' }
const drafts = reactive<Record<string, Draft>>({})
const MB = 1024 * 1024
const GB = 1024 * MB
const isBytes = (s: PlatformSetting) => s.unit === 'bytes'
const fmtBytes = (n: number) => (n >= GB && n % (GB / 100) === 0 ? `${+(n / GB).toFixed(2)} GB` : n >= MB ? `${+(n / MB).toFixed(1)} MB` : `${n} bytes`)
const fmtVal = (s: PlatformSetting, v: number | boolean) =>
  typeof v === 'boolean' ? (v ? 'on' : 'off') : isBytes(s) ? fmtBytes(v) : `${v}${s.unit ? ' ' + s.unit : ''}`

function seed(s: PlatformSetting) {
  const v = s.value
  if (typeof v === 'boolean') drafts[s.key] = { text: '', on: v, unit: 'MB' }
  else if (isBytes(s)) {
    const gb = v >= GB && v % GB === 0
    drafts[s.key] = { text: String(gb ? v / GB : +(v / MB).toFixed(2)), on: false, unit: gb ? 'GB' : 'MB' }
  } else drafts[s.key] = { text: String(v), on: false, unit: 'MB' }
}
function apply(g: SettingsGroup[]) {
  groups.value = g
  for (const grp of g) for (const s of grp.settings) seed(s)
}
async function load() {
  try {
    apply((await getSettings()).groups)
    loadError.value = ''
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : "Couldn't load settings. Reload to try again."
  } finally {
    loaded.value = true
  }
}
onMounted(load)

// Parsed draft value, or an error string.
function parsed(s: PlatformSetting): { value: number | boolean } | { error: string } {
  const d = drafts[s.key]
  if (!d) return { value: s.value }
  if (s.type === 'bool') return { value: d.on }
  const t = d.text.trim()
  if (!/^\d+(\.\d+)?$/.test(t)) return { error: 'Enter a whole number.' }
  const n = isBytes(s) ? Math.round(Number(t) * (d.unit === 'GB' ? GB : MB)) : Number(t)
  if (!isBytes(s) && !Number.isInteger(n)) return { error: 'Enter a whole number.' }
  if (s.min !== null && n < s.min) return { error: `At least ${fmtVal(s, s.min)}.` }
  if (s.max !== null && n > s.max) return { error: `At most ${fmtVal(s, s.max)}.` }
  return { value: n }
}
const errorOf = (s: PlatformSetting) => {
  const p = parsed(s)
  return 'error' in p ? p.error : ''
}
const changed = (s: PlatformSetting) => {
  const p = parsed(s)
  return 'error' in p || p.value !== s.value
}
const dirtyIn = (g: SettingsGroup) => g.settings.filter(changed)
const invalidIn = (g: SettingsGroup) => g.settings.some(errorOf)
const rangeHelp = (s: PlatformSetting) => {
  if (s.type === 'bool') return ''
  const lo = s.min !== null ? fmtVal(s, s.min) : ''
  const hi = s.max !== null ? fmtVal(s, s.max) : ''
  return lo && hi ? `Allowed: ${lo} to ${hi}.` : hi ? `Up to ${hi}.` : lo ? `At least ${lo}.` : ''
}

async function save(g: SettingsGroup) {
  const body: Record<string, number | boolean | null> = {}
  for (const s of dirtyIn(g)) {
    const p = parsed(s)
    if ('value' in p) body[s.key] = p.value
  }
  if (!Object.keys(body).length) return
  await send(g.name, body, `${g.name} saved.`)
}
async function reset(g: SettingsGroup, s: PlatformSetting) {
  await send(g.name, { [s.key]: null }, `${s.label} reset to its default.`)
}
async function send(group: string, body: Record<string, number | boolean | null>, ok: string) {
  busy.value = group
  groupError[group] = ''
  try {
    apply((await putSettings(body)).groups)
    toast.success(ok)
  } catch (e) {
    groupError[group] = e instanceof ApiError ? e.message : "Couldn't save. Try again."
  } finally {
    busy.value = null
  }
}
const canSave = computed(() => (g: SettingsGroup) => busy.value === null && dirtyIn(g).length > 0 && !invalidIn(g))
</script>

<template>
  <div class="space-y-3">
    <div v-if="loadError" role="alert" class="alert-error">{{ loadError }}</div>
    <p v-else-if="!loaded" class="text-sm text-zinc-400" role="status">Loading settings…</p>
    <p v-else class="help">
      Platform-wide settings. Ceilings cap how high an admin can raise one app's limits (Apps tab, Limits). Changes apply within a few seconds.
      Clearing a value back to its default removes the override.
    </p>

    <section v-for="g in groups" :key="g.name" class="card" :aria-labelledby="`${uid}-${g.name}`">
      <div class="border-b border-pico-border px-3 py-2">
        <h2 :id="`${uid}-${g.name}`" class="section-title">{{ g.name }}</h2>
      </div>
      <ul class="divide-y divide-pico-border">
        <li v-for="s in g.settings" :key="s.key" class="grid gap-2 px-3 py-3 sm:grid-cols-[minmax(0,1fr)_16rem] sm:gap-4">
          <div class="min-w-0">
            <label :for="`${uid}-${s.key}`" class="label !mb-0 flex flex-wrap items-center gap-2">
              {{ s.label }}
              <span v-if="s.overridden" class="badge-warn">overridden</span>
            </label>
            <p class="help">{{ s.description }}</p>
            <p class="meta mt-0.5">
              Default {{ fmtVal(s, s.default) }}<template v-if="rangeHelp(s)"> · {{ rangeHelp(s) }}</template>
              <template v-if="s.overridden && s.updated_at"> · changed {{ stamp(s.updated_at) }}<template v-if="s.updated_by"> by {{ s.updated_by }}</template></template>
            </p>
          </div>
          <div class="min-w-0">
            <label v-if="s.type === 'bool'" class="check min-h-[36px]">
              <input :id="`${uid}-${s.key}`" v-model="drafts[s.key].on" type="checkbox" />
              <span>{{ drafts[s.key].on ? 'On' : 'Off' }}</span>
            </label>
            <div v-else class="flex items-center gap-2">
              <input
                :id="`${uid}-${s.key}`" v-model="drafts[s.key].text" type="text" inputmode="decimal" class="input mono min-w-0 flex-1"
                :aria-invalid="errorOf(s) ? 'true' : undefined" :aria-describedby="errorOf(s) ? `${uid}-${s.key}-err` : undefined"
                @keydown.enter.prevent="canSave(g) && save(g)"
              />
              <select v-if="isBytes(s)" v-model="drafts[s.key].unit" class="input w-20 shrink-0" :aria-label="`Unit for ${s.label}`">
                <option>MB</option><option>GB</option>
              </select>
              <span v-else-if="s.unit" class="meta shrink-0">{{ s.unit }}</span>
            </div>
            <p v-if="errorOf(s)" :id="`${uid}-${s.key}-err`" class="field-error">{{ errorOf(s) }}</p>
            <button
              v-if="s.overridden" type="button" class="btn-ghost btn-sm mt-1" :disabled="busy !== null"
              :aria-label="`Reset ${s.label} to default`" @click="reset(g, s)"
            >Reset to default ({{ fmtVal(s, s.default) }})</button>
          </div>
        </li>
      </ul>
      <div class="flex flex-wrap items-center gap-3 border-t border-pico-border px-3 py-2">
        <button type="button" class="btn-primary btn-sm" :disabled="!canSave(g)" @click="save(g)">{{ busy === g.name ? 'Saving…' : `Save ${g.name.toLowerCase()}` }}</button>
        <span v-if="dirtyIn(g).length" class="meta">{{ dirtyIn(g).length }} unsaved {{ dirtyIn(g).length === 1 ? 'change' : 'changes' }}</span>
        <p v-if="groupError[g.name]" role="alert" class="text-sm text-status-error">{{ groupError[g.name] }}</p>
      </div>
    </section>
  </div>
</template>
