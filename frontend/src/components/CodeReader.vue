<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import CopyButton from './CopyButton.vue'
import IconButton from './IconButton.vue'
import { ApiError } from '../api/client'
import { fmtSize } from '../composables/kaisen'
import { useModal } from '../composables/useModal'

// Full-viewport reader: collapsible folder tree with a filter on the left, a wide monospace pane with line numbers on the right.
// Used for project files and for diffs. The caller supplies the entries and a loader; nothing here knows the API.
export interface ReaderItem { path: string; size?: number; tag?: string }
export interface ReaderContent { text: string | null; note?: string }

const props = defineProps<{
  open: boolean
  title: string
  items: ReaderItem[]
  load: (path: string) => Promise<ReaderContent>
  diff?: boolean // colour lines by their first character
  initialPath?: string
}>()
const emit = defineEmits<{ close: [] }>()

const dlg = ref<HTMLDialogElement | null>(null)
const m = useModal(dlg, () => props.open, () => emit('close'))

type Row = { key: string; path: string; name: string; depth: number; folder: boolean; open?: boolean; item?: ReaderItem }
const collapsed = ref(new Set<string>())
const filter = ref('')
const cursor = ref(0)
const selected = ref('')
const content = ref<ReaderContent | null>(null)
const loading = ref(false)
const error = ref('')
const wrap = ref(false)
const treeOpen = ref(true) // phones: the tree stacks above the pane and can be folded away
const cache = new Map<string, ReaderContent>()
const treeEl = ref<HTMLElement | null>(null)

interface Node { name: string; path: string; children: Map<string, Node>; item?: ReaderItem }
const rows = computed<Row[]>(() => {
  const q = filter.value.trim().toLowerCase()
  if (q) {
    return props.items
      .filter((i) => i.path.toLowerCase().includes(q))
      .map((i) => ({ key: i.path, path: i.path, name: i.path, depth: 0, folder: false, item: i }))
  }
  const root: Node = { name: '', path: '', children: new Map() }
  for (const it of props.items) {
    let n = root
    const parts = it.path.split('/')
    parts.forEach((p, idx) => {
      const path = parts.slice(0, idx + 1).join('/')
      if (!n.children.has(p)) n.children.set(p, { name: p, path, children: new Map() })
      n = n.children.get(p)!
      if (idx === parts.length - 1) n.item = it
    })
  }
  const out: Row[] = []
  const walk = (n: Node, depth: number) => {
    const kids = [...n.children.values()].sort((a, b) => Number(!!a.item && !a.children.size) - Number(!!b.item && !b.children.size) || a.name.localeCompare(b.name))
    for (const k of kids) {
      const folder = k.children.size > 0
      const open = folder && !collapsed.value.has(k.path)
      out.push({ key: k.path, path: k.path, name: k.name, depth, folder, open, item: folder ? undefined : k.item })
      if (open) walk(k, depth + 1)
    }
  }
  walk(root, 0)
  return out
})
const current = computed(() => props.items.find((i) => i.path === selected.value))
const lines = computed(() => (content.value?.text != null ? content.value.text.replace(/\n$/, '').split('\n') : []))
const gutter = computed(() => String(lines.value.length).length)
const allFolders = computed(() => {
  const s = new Set<string>()
  for (const i of props.items) { const p = i.path.split('/'); for (let k = 1; k < p.length; k++) s.add(p.slice(0, k).join('/')) }
  return s
})

async function openFile(path: string) {
  selected.value = path
  error.value = ''
  const hit = cache.get(path)
  if (hit) { content.value = hit; loading.value = false; return }
  content.value = null
  loading.value = true
  try {
    const c = await props.load(path)
    cache.set(path, c)
    if (selected.value === path) content.value = c
  } catch (e) {
    if (selected.value === path) error.value = e instanceof ApiError ? e.message : "Couldn't load the file."
  } finally {
    if (selected.value === path) loading.value = false
  }
}
const lineCls = (l: string) =>
  !props.diff ? ''
  : l.startsWith('+') && !l.startsWith('+++') ? 'text-status-running'
  : l.startsWith('-') && !l.startsWith('---') ? 'text-danger-light'
  : l.startsWith('@@') ? 'text-accent-light' : ''

function activate(i: number) {
  const r = rows.value[i]
  if (!r) return
  cursor.value = i
  if (r.folder) {
    toggle(r)
  } else if (r.item) openFile(r.path)
}
function toggle(r: Row) {
  const s = new Set(collapsed.value)
  if (s.has(r.path)) s.delete(r.path); else s.add(r.path)
  collapsed.value = s
}
function move(to: number, openIt = true) {
  const i = Math.max(0, Math.min(rows.value.length - 1, to))
  cursor.value = i
  const r = rows.value[i]
  if (openIt && r && !r.folder && r.item) openFile(r.path)
  nextTick(() => document.getElementById(rowId(i))?.scrollIntoView({ block: 'nearest' }))
}
const rowId = (i: number) => `cr-row-${i}`
function onTreeKey(e: KeyboardEvent) {
  if (e.ctrlKey || e.metaKey || e.altKey) return
  const r = rows.value[cursor.value]
  switch (e.key) {
    case 'ArrowDown': case 'j': move(cursor.value + 1); break
    case 'ArrowUp': case 'k': move(cursor.value - 1); break
    case 'Home': move(0); break
    case 'End': move(rows.value.length - 1); break
    case 'ArrowRight': case 'l':
      if (r?.folder && !r.open) toggle(r)
      else if (r?.folder) move(cursor.value + 1)
      else if (r?.item) openFile(r.path)
      break
    case 'ArrowLeft': case 'h':
      if (r?.folder && r.open) toggle(r)
      else if (r && r.depth > 0 && !filter.value) {
        for (let i = cursor.value - 1; i >= 0; i--) if (rows.value[i].folder && rows.value[i].depth < r.depth) { move(i, false); break }
      }
      break
    case 'Enter': case ' ': activate(cursor.value); break
    case '/': document.getElementById('cr-filter')?.focus(); break
    default: return
  }
  e.preventDefault()
}
function onFilterKey(e: KeyboardEvent) {
  if (e.key === 'ArrowDown') { e.preventDefault(); treeEl.value?.focus(); move(0) }
}
watch(filter, () => { cursor.value = 0 })

watch(() => props.open, (o) => {
  if (!o) return
  filter.value = ''
  const first = props.initialPath && props.items.some((i) => i.path === props.initialPath) ? props.initialPath : props.items[0]?.path
  cache.clear()
  collapsed.value = new Set()
  if (first) {
    const p = first.split('/')
    for (let k = 1; k < p.length; k++) collapsed.value.delete(p.slice(0, k).join('/'))
    nextTick(() => { const i = rows.value.findIndex((r) => r.path === first); cursor.value = Math.max(0, i); openFile(first) })
  } else { selected.value = ''; content.value = null }
}, { immediate: true })
watch(() => props.items, () => cache.clear())

const collapseAll = () => { collapsed.value = new Set(allFolders.value) }
const expandAll = () => { collapsed.value = new Set() }
const fileCount = computed(() => props.items.length)
</script>

<template>
  <dialog
    ref="dlg"
    :aria-label="title"
    class="fixed inset-0 m-0 h-dvh max-h-dvh w-screen max-w-none overflow-hidden bg-pico-panel p-0 text-white backdrop:bg-black/60"
    @cancel="m.onNativeCancel"
  >
    <div class="flex h-dvh flex-col">
      <header class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-pico-border px-3 py-2">
        <h2 class="section-title">{{ title }} <span class="mono font-normal text-zinc-400">{{ fileCount }}</span></h2>
        <span class="meta max-md:hidden">j/k or arrows move, / filters, Esc closes</span>
        <button type="button" class="row-act md:hidden" :aria-expanded="treeOpen" aria-controls="cr-tree-wrap" @click="treeOpen = !treeOpen">{{ treeOpen ? 'Hide file list' : 'Show file list' }}</button>
        <IconButton class="ml-auto" label="Close reader" text="Close" @click="emit('close')" />
      </header>

      <div class="flex min-h-0 flex-1 flex-col md:flex-row">
        <div v-show="treeOpen" id="cr-tree-wrap" class="flex min-h-0 shrink-0 flex-col border-b border-pico-border max-md:max-h-[40dvh] md:w-80 md:border-b-0 md:border-r">
          <div class="flex items-center gap-2 border-b border-pico-border px-2 py-1.5">
            <label for="cr-filter" class="sr-only">Filter files</label>
            <input id="cr-filter" v-model="filter" type="search" class="input mono min-w-0 flex-1" placeholder="Filter files" autocomplete="off" @keydown="onFilterKey" />
            <template v-if="!filter && allFolders.size">
              <button type="button" class="row-act" @click="collapseAll">Collapse</button>
              <button type="button" class="row-act" @click="expandAll">Expand</button>
            </template>
          </div>
          <div
            ref="treeEl" role="tree" tabindex="0" data-autofocus aria-label="Files" :aria-activedescendant="rows.length ? rowId(cursor) : undefined"
            class="min-h-0 flex-1 overflow-y-auto py-1 focus-visible:outline-offset-[-2px]" @keydown="onTreeKey"
          >
            <p v-if="items.length === 0" class="px-3 py-2 text-sm text-zinc-300">Nothing here yet.</p>
            <p v-else-if="rows.length === 0" class="px-3 py-2 text-sm text-zinc-300">No file matches "{{ filter }}".</p>
            <div
              v-for="(r, i) in rows" :id="rowId(i)" :key="r.key" role="treeitem" :aria-level="r.depth + 1" :aria-expanded="r.folder ? r.open : undefined"
              :aria-selected="!r.folder && selected === r.path"
              class="mono flex min-h-[26px] cursor-pointer items-center gap-1.5 px-2 text-sm hover:bg-pico-card max-md:min-h-[40px]"
              :class="[cursor === i ? 'bg-pico-card' : '', !r.folder && selected === r.path ? 'font-semibold text-white' : 'text-zinc-300']"
              :style="{ paddingLeft: `${8 + r.depth * 14}px` }" :title="r.path" tabindex="-1" @click="activate(i)" @keydown.enter.prevent.stop="activate(i)"
            >
              <span v-if="r.folder" class="w-3 shrink-0 text-zinc-400" aria-hidden="true">{{ r.open ? 'v' : '>' }}</span>
              <span v-else class="w-3 shrink-0" aria-hidden="true" />
              <span class="min-w-0 flex-1 truncate">{{ r.name }}{{ r.folder ? '/' : '' }}</span>
              <span v-if="r.item?.tag" class="shrink-0 text-xs text-zinc-400">{{ r.item.tag }}</span>
              <span v-else-if="r.item?.size !== undefined" class="shrink-0 text-xs text-zinc-400">{{ fmtSize(r.item.size) }}</span>
            </div>
          </div>
        </div>

        <section class="flex min-h-0 min-w-0 flex-1 flex-col" aria-label="File contents" aria-live="polite">
          <p v-if="!selected" class="p-4 text-sm text-zinc-300">{{ items.length ? 'Select a file to read it.' : 'Nothing to read yet.' }}</p>
          <template v-else>
            <div class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-pico-border px-3 py-1.5">
              <span class="mono min-w-0 flex-1 break-all text-sm" data-testid="reader-path">{{ selected }}</span>
              <span v-if="current?.size !== undefined" class="mono text-xs text-zinc-400">{{ fmtSize(current.size) }}</span>
              <span v-if="lines.length" class="mono text-xs text-zinc-400">{{ lines.length }} lines</span>
              <label class="flex items-center gap-1.5 text-sm text-zinc-200"><input v-model="wrap" type="checkbox" class="check" /> Wrap</label>
              <CopyButton v-if="content && content.text" :text="content.text" label="Copy file" />
            </div>
            <div v-if="loading" class="m-3 h-40 animate-pulse rounded-sm bg-pico-card" aria-busy="true" aria-label="Loading file" />
            <div v-else-if="error" role="alert" class="alert-error m-3 flex flex-wrap items-center justify-between gap-2">
              <span>{{ error }}</span>
              <IconButton label="Retry" @click="openFile(selected)" />
            </div>
            <p v-else-if="content && content.text === null" class="p-3 text-sm text-zinc-300">{{ content.note || 'This file is too large to preview. Download the code to see it.' }}</p>
            <div v-else-if="content" class="min-h-0 flex-1 overflow-auto bg-pico-bg" tabindex="0" :aria-label="`Contents of ${selected}`" data-testid="reader-pane">
              <div class="mono w-max min-w-full py-2 text-[13px] leading-5" :class="wrap ? '!w-full' : ''">
                <div v-for="(l, i) in lines" :key="i" class="flex" :class="lineCls(l)">
                  <span class="sticky left-0 shrink-0 select-none bg-pico-bg px-3 text-right text-zinc-500" :style="{ minWidth: `${gutter + 3}ch` }" aria-hidden="true">{{ i + 1 }}</span>
                  <span class="min-w-0 flex-1 pr-4" :class="wrap ? 'whitespace-pre-wrap break-all' : 'whitespace-pre'">{{ l || ' ' }}</span>
                </div>
              </div>
            </div>
          </template>
        </section>
      </div>
    </div>
  </dialog>
</template>
