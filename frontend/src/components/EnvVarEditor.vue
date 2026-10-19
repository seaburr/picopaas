<script setup lang="ts">
import { reactive, watch } from 'vue'
import { DB_REFS, MASKED_VALUE, STORAGE_REFS, type EnvVar } from '../types'

const props = defineProps<{
  modelValue: EnvVar[]
  wantDb: boolean
  wantStorage: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [EnvVar[]] }>()

// Local row shape adds `touched`: whether the user has actually edited this
// row's value since load. A secret row loaded from the API only ever carries
// the masked placeholder (app/schemas.py MASKED_VALUE) — we never see the
// real value — so untouched secret rows are re-sent as that same placeholder.
//
// KNOWN GAP (see docs/LEDGER.md backend-dev note): AppUpdate.env replaces the
// whole env list, so there is no server-side "leave this one alone". Until
// the backend supports a partial env patch, saving an edit that doesn't touch
// a given secret re-sends the masked placeholder string as its value, which
// is harmless today (the reconciler isn't live yet / status stays "pending")
// but would overwrite the real secret once it is. Flagged here deliberately.
interface Row extends EnvVar {
  touched: boolean
}

let nextId = 0
const rowId = new WeakMap<Row, number>()
function idFor(row: Row): number {
  if (!rowId.has(row)) rowId.set(row, nextId++)
  return rowId.get(row)!
}

const rows = reactive<Row[]>(
  props.modelValue.map((v) => ({ ...v, touched: v.is_secret ? false : true })),
)

let activeIndex: number | null = null

function emitChange() {
  const payload: EnvVar[] = rows.map((r) => ({
    key: r.key,
    value: r.is_secret && !r.touched ? MASKED_VALUE : r.value,
    is_secret: r.is_secret,
  }))
  emit('update:modelValue', payload)
}

watch(rows, emitChange, { deep: true })

function addRow() {
  rows.push({ key: '', value: '', is_secret: false, touched: true })
}

function removeRow(i: number) {
  rows.splice(i, 1)
}

function startChangingSecret(i: number) {
  rows[i].value = ''
  rows[i].touched = true
}

function onToggleSecret(i: number) {
  const row = rows[i]
  if (row.is_secret && !row.touched) {
    // Flipping a masked, untouched row to "secret" or away from it needs a
    // real value either way — clear it and force the user to retype.
    row.value = ''
    row.touched = true
  }
}

function setActive(i: number) {
  activeIndex = i
}

function refLabel(ref: string): string {
  return '${{ ' + ref + ' }}'
}

function insertRef(ref: string) {
  const value = refLabel(ref)
  if (activeIndex !== null && rows[activeIndex]) {
    rows[activeIndex].value = value
    rows[activeIndex].touched = true
  } else {
    rows.push({ key: '', value, is_secret: false, touched: true })
  }
}

defineExpose({ addRow })
</script>

<template>
  <div class="space-y-3">
    <div v-if="rows.length === 0" class="text-sm text-zinc-500">No env vars yet.</div>

    <div
      v-for="(row, i) in rows"
      :key="idFor(row)"
      class="flex flex-wrap items-start gap-2 rounded-md border border-pico-border bg-pico-card p-2"
    >
      <input
        v-model="row.key"
        type="text"
        placeholder="KEY"
        class="w-40 rounded border border-pico-border bg-pico-panel px-2 py-1.5 font-mono text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-accent"
      />

      <template v-if="row.is_secret && !row.touched">
        <input
          type="password"
          value="********"
          disabled
          class="min-w-[10rem] flex-1 rounded border border-pico-border bg-pico-panel/50 px-2 py-1.5 font-mono text-sm text-zinc-500"
        />
        <button
          type="button"
          class="rounded border border-pico-border px-2 py-1.5 text-xs text-zinc-300 hover:bg-pico-panel"
          @click="startChangingSecret(i)"
        >
          Change
        </button>
      </template>
      <input
        v-else
        v-model="row.value"
        :type="row.is_secret ? 'password' : 'text'"
        placeholder="value"
        class="min-w-[10rem] flex-1 rounded border border-pico-border bg-pico-panel px-2 py-1.5 font-mono text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-accent"
        @focus="setActive(i)"
      />

      <label class="flex items-center gap-1.5 whitespace-nowrap text-xs text-zinc-400">
        <input
          type="checkbox"
          :checked="row.is_secret"
          class="accent-accent"
          @change="row.is_secret = !row.is_secret; onToggleSecret(i)"
        />
        secret
      </label>

      <button
        type="button"
        class="rounded border border-pico-border px-2 py-1.5 text-xs text-danger hover:bg-danger/10"
        @click="removeRow(i)"
      >
        Remove
      </button>
    </div>

    <button
      type="button"
      class="rounded border border-pico-border px-3 py-1.5 text-sm text-zinc-300 hover:bg-pico-card"
      @click="addRow"
    >
      + Add env var
    </button>

    <div v-if="wantDb || wantStorage" class="rounded-md border border-pico-border bg-pico-card p-3">
      <p class="mb-2 text-xs text-zinc-500">
        Reference variables — click to insert into the last-focused value field (or add a new row).
      </p>
      <div class="flex flex-wrap gap-1.5">
        <button
          v-for="ref in [...(wantDb ? DB_REFS : []), ...(wantStorage ? STORAGE_REFS : [])]"
          :key="ref"
          type="button"
          class="rounded border border-pico-border bg-pico-panel px-2 py-1 font-mono text-xs text-accent-light hover:border-accent"
          @click="insertRef(ref)"
        >
          {{ refLabel(ref) }}
        </button>
      </div>
    </div>
  </div>
</template>
