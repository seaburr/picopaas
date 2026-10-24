<script setup lang="ts">
import { Eye, EyeOff, Pencil, Trash2 } from 'lucide-vue-next'
import IconButton from './IconButton.vue'
import { computed, reactive, watch } from 'vue'
import { envKeyError, REF_TIPS, refLabel, SUGGESTED_KEYS } from '../composables/envKeys'
import { DB_REFS, MASKED_VALUE, STORAGE_REFS, type EnvVar } from '../types'

const props = defineProps<{
  modelValue: EnvVar[]
  wantDb: boolean
  wantStorage: boolean
  // Server-side errors by dotted path from the API, re-indexed to editor rows: "<row>.key" / "<row>.value".
  errors?: Record<string, string>
}>()
const emit = defineEmits<{ 'update:modelValue': [EnvVar[]] }>()

// `touched`: the user edited this row's value since load. A secret row loaded from the API only carries the masked
// placeholder (app/schemas.py MASKED_VALUE), so untouched secret rows are re-sent as that same placeholder.
interface Row extends EnvVar {
  touched: boolean
  peek?: boolean // show the typed secret value (never offered for the saved mask)
}

let nextId = 0
const rowId = new WeakMap<Row, number>()
function idFor(row: Row): number {
  if (!rowId.has(row)) rowId.set(row, nextId++)
  return rowId.get(row)!
}

const rows = reactive<Row[]>(props.modelValue.map((v) => ({ ...v, touched: !v.is_secret })))
let activeIndex: number | null = null

function emitChange() {
  emit(
    'update:modelValue',
    rows.map((r) => ({ key: r.key, value: r.is_secret && !r.touched ? MASKED_VALUE : r.value, is_secret: r.is_secret })),
  )
}
watch(rows, emitChange, { deep: true })

const dupes = computed(() => {
  const count: Record<string, number> = {}
  for (const r of rows) if (r.key) count[r.key] = (count[r.key] ?? 0) + 1
  return new Set(Object.keys(count).filter((k) => count[k] > 1))
})

const blank = (r: Row) => !r.key && !r.value
function keyMsg(r: Row, i: number): string {
  if (props.errors?.[`${i}.key`]) return props.errors[`${i}.key`]
  if (blank(r)) return ''
  if (!r.key) return 'Name is required.'
  return r.key ? envKeyError(r.key) : ''
}
function valueMsg(i: number): string {
  return props.errors?.[`${i}.value`] ?? ''
}

function addRow(key = '', value = '') {
  rows.push({ key, value, is_secret: false, touched: true })
  activeIndex = null
}
function removeRow(i: number) {
  rows.splice(i, 1)
  activeIndex = null
}
function startChangingSecret(i: number) {
  rows[i].value = ''
  rows[i].touched = true
}
function onToggleSecret(i: number) {
  const row = rows[i]
  row.is_secret = !row.is_secret
  if (!row.touched) {
    // Flipping a masked row needs a real value either way: clear it and force a retype.
    row.value = ''
    row.touched = true
  }
}
function onKeyInput(i: number, e: Event) {
  rows[i].key = (e.target as HTMLInputElement).value.toUpperCase().replace(/\s+/g, '_')
}

function insertRef(ref: string) {
  const value = refLabel(ref)
  const r = activeIndex !== null ? rows[activeIndex] : undefined
  if (r && !r.value && !(r.is_secret && !r.touched)) {
    r.value = value
    r.touched = true
    if (!r.key) r.key = SUGGESTED_KEYS[ref] ?? ''
  } else if (!r && rows.length === 1 && blank(rows[0])) {
    rows[0].key = SUGGESTED_KEYS[ref] ?? ''
    rows[0].value = value
  } else {
    addRow(SUGGESTED_KEYS[ref] ?? '', value)
  }
}

const envGrid = 'sm:grid sm:grid-cols-[14rem_minmax(0,1fr)_6rem] sm:items-center sm:gap-2'
const groups = computed(() => [
  ...(props.wantDb ? [{ title: 'Database', refs: DB_REFS }] : []),
  ...(props.wantStorage ? [{ title: 'Blob storage', refs: STORAGE_REFS }] : []),
])
const explainer = 'Use ' + refLabel('db.url') + ' references to inject database and blob storage credentials.'
</script>

<template>
  <div class="space-y-3">
    <p class="help !mt-0">
      Plain values are visible to you. Mark a value <strong class="font-medium text-zinc-200">secret</strong> to encrypt it; it can't be shown again.
      {{ explainer }}
    </p>

    <p v-if="rows.length === 0" class="rounded-md border border-dashed border-pico-border px-3 py-4 text-center text-sm text-zinc-400">
      No env vars yet.
    </p>

    <div v-if="rows.length" :class="[envGrid, 'table-th hidden px-2']"><span>Key</span><span>Value</span><span class="text-right">Secret</span></div>
    <div v-for="(row, i) in rows" :key="idFor(row)" class="rounded-md border border-pico-border bg-pico-card p-1.5">
      <div :class="['flex flex-col gap-1.5', envGrid]">
        <div>
          <input
            :value="row.key"
            type="text"
            placeholder="KEY"
            autocapitalize="characters"
            autocomplete="off"
            spellcheck="false"
            :aria-label="`Name of variable ${i + 1}`"
            :aria-invalid="!!keyMsg(row, i) || undefined"
            class="input mono bg-pico-panel"
            @input="onKeyInput(i, $event)"
            @focus="activeIndex = null"
          />
        </div>

        <div class="min-w-0 flex-1">
          <div v-if="row.is_secret && !row.touched" class="flex gap-1">
            <input type="password" value="********" disabled :aria-label="`Value for ${row.key || 'secret'} (hidden)`" class="input mono bg-pico-panel/50" />
            <IconButton :icon="Pencil" :label="`Change secret ${row.key}`" @click="startChangingSecret(i)" />
          </div>
          <div v-else class="relative">
            <input
              v-model="row.value"
              :type="row.is_secret && !row.peek ? 'password' : 'text'"
              placeholder="value"
              autocomplete="off"
              :aria-label="`Value for ${row.key || 'variable ' + (i + 1)}`"
              :aria-invalid="!!valueMsg(i) || undefined"
              :class="['input mono bg-pico-panel', row.is_secret ? 'pr-9' : '']"
              @focus="activeIndex = i"
            />
            <button
              v-if="row.is_secret"
              type="button"
              class="btn-icon btn-icon-sm absolute right-0.5 top-0.5"
              :aria-pressed="!!row.peek"
              :aria-label="row.peek ? `Hide value for ${row.key || 'variable ' + (i + 1)}` : `Show value for ${row.key || 'variable ' + (i + 1)}`"
              :title="row.peek ? 'Hide value' : 'Show value'"
              @mousedown.prevent
              @click="row.peek = !row.peek"
            >
              <EyeOff v-if="row.peek" :size="16" aria-hidden="true" />
              <Eye v-else :size="16" aria-hidden="true" />
            </button>
          </div>
        </div>

        <div class="flex items-center justify-between gap-2 sm:justify-end">
          <label class="flex items-center gap-1.5 whitespace-nowrap text-xs text-zinc-300">
            <input type="checkbox" :checked="row.is_secret" class="check" @change="onToggleSecret(i)" />
            secret
          </label>
          <IconButton :icon="Trash2" :label="`Remove ${row.key || 'variable ' + (i + 1)}`" danger @click="removeRow(i)" />
        </div>
      </div>
      <p v-if="keyMsg(row, i)" class="field-error" role="alert">{{ keyMsg(row, i) }}</p>
      <p v-else-if="row.key && dupes.has(row.key)" class="mt-1 text-xs text-amber-300">{{ row.key }} is used more than once; only the last one is kept.</p>
      <p v-if="valueMsg(i)" class="field-error" role="alert">{{ valueMsg(i) }}</p>
    </div>

    <button type="button" class="btn-secondary btn-sm" @click="addRow()">+ Add env var</button>

    <div v-if="groups.length" class="rounded-md border border-pico-border bg-pico-card p-3">
      <p class="mb-2 text-xs text-zinc-300">
        Click a reference to put it in the focused empty value field, or add a new row with a suggested name.
      </p>
      <div v-for="g in groups" :key="g.title" class="mt-2 first:mt-0">
        <p class="mb-1 text-xs font-medium uppercase tracking-wide text-zinc-400">{{ g.title }}</p>
        <div class="flex flex-wrap gap-1.5">
          <button
            v-for="ref in g.refs"
            :key="ref"
            type="button"
            :title="REF_TIPS[ref] ?? SUGGESTED_KEYS[ref]"
            class="rounded border border-pico-border bg-pico-panel px-1.5 py-0.5 mono text-accent-light hover:border-accent"
            @mousedown.prevent
            @click="insertRef(ref)"
          >
            {{ refLabel(ref) }}
          </button>
        </div>
      </div>
    </div>
    <p v-else class="help">Tick Database or Blob storage above to get <code class="mono">{{ refLabel('…') }}</code> references for their credentials.</p>
  </div>
</template>
