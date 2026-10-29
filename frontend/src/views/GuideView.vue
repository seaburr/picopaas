<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import DOMPurify from 'dompurify'
import { Marked } from 'marked'
import userMd from '@docs/USER_GUIDE.md?raw'
import apiMd from '@docs/API.md?raw'
import cliMd from '@docs/CLI.md?raw'
import recipesMd from '@docs/RECIPES.md?raw'
import NavBar from '../components/NavBar.vue'
import { getAdminGuide } from '../api/admin'
import { useAuthStore } from '../stores/auth'

const PUBLIC_DOCS = {
  user: { title: 'User guide', md: userMd },
  api: { title: 'API', md: apiMd },
  cli: { title: 'CLI', md: cliMd },
  recipes: { title: 'Recipes', md: recipesMd },
} as const
type DocKey = keyof typeof PUBLIC_DOCS | 'admin'

const auth = useAuthStore()
const isAdmin = computed(() => auth.user?.role === 'platform_admin')
const DOCS = computed<Record<string, { title: string }>>(() => ({
  ...PUBLIC_DOCS,
  ...(isAdmin.value ? { admin: { title: 'Admin' } } : {}),
}))

const route = useRoute()
const doc = computed<DocKey>(() => (route.path === '/guide/admin' ? 'admin' : (route.params.doc as DocKey) || 'user'))

// The admin guide is never bundled: fetched from the admin-only endpoint.
const adminMd = ref('')
const adminLoading = ref(false)
const adminError = ref('')
async function loadAdmin() {
  adminLoading.value = true
  adminError.value = ''
  try {
    adminMd.value = (await getAdminGuide()).markdown
  } catch (e) {
    adminError.value = e instanceof Error ? e.message : 'Failed to load the admin guide'
  } finally {
    adminLoading.value = false
  }
}
watch(doc, (d) => { if (d === 'admin' && !adminMd.value) void loadAdmin() }, { immediate: true })
const currentMd = computed(() => (doc.value === 'admin' ? adminMd.value : PUBLIC_DOCS[doc.value].md))
const navOpen = ref(false)

const slug = (t: string) =>
  t.toLowerCase().replace(/[`*_]/g, '').replace(/[^a-z0-9 -]/g, '').trim().replace(/[ -]+/g, '-')
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const LINKS: Record<string, string> = {
  'USER_GUIDE.md': '/guide/user',
  'API.md': '/guide/api',
  'CLI.md': '/guide/cli',
  'RECIPES.md': '/guide/recipes',
}

interface Toc { level: number; text: string; id: string }

const rendered = computed(() => {
  const toc: Toc[] = []
  const m = new Marked({
    renderer: {
      heading({ tokens, depth }) {
        const raw = tokens.map((t) => ('text' in t ? String(t.text) : '')).join('')
        const id = slug(raw)
        if (depth === 2 || depth === 3) toc.push({ level: depth, text: raw.replace(/`/g, ''), id })
        return `<h${depth} id="${id}"><a class="anchor" href="#${id}">#</a>${this.parser.parseInline(tokens)}</h${depth}>`
      },
      code({ text }) {
        return `<div class="code"><button type="button" class="copy-btn">Copy</button><pre><code>${esc(text)}</code></pre></div>`
      },
      table(token) {
        const cell = (c: { tokens: never[] }) => this.parser.parseInline(c.tokens)
        const head = token.header.map((c) => `<th>${cell(c as never)}</th>`).join('')
        const rows = token.rows.map((r) => `<tr>${r.map((c) => `<td>${cell(c as never)}</td>`).join('')}</tr>`).join('')
        return `<div class="tbl-wrap"><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>`
      },
      link({ href, tokens }) {
        const [file, hash] = href.split('#')
        const target = LINKS[file.replace(/^(\.\/|\.\.\/|docs\/)+/, '')]
        const h = target ? target + (hash ? '#' + hash : '') : href
        return `<a href="${esc(h)}">${this.parser.parseInline(tokens)}</a>`
      },
    },
  })
  const html = DOMPurify.sanitize(m.parse(currentMd.value, { async: false }) as string)
  return { html, toc }
})

// Active section: the last h2/h3 whose top has scrolled past the top of the viewport.
const activeId = ref('')
let raf = 0
function updateActive() {
  raf = 0
  const heads = document.querySelectorAll<HTMLElement>('.doc h2[id], .doc h3[id]')
  let cur = ''
  for (const h of heads) {
    if (h.getBoundingClientRect().top <= 90) cur = h.id
    else break
  }
  activeId.value = cur
}
function onScroll() {
  if (!raf) raf = requestAnimationFrame(updateActive)
}
onMounted(() => {
  window.addEventListener('scroll', onScroll, { passive: true })
  nextTick(updateActive)
})
onBeforeUnmount(() => {
  window.removeEventListener('scroll', onScroll)
  if (raf) cancelAnimationFrame(raf)
})
watch(rendered, () => nextTick(updateActive))
const activeText = computed(() => rendered.value.toc.find((t) => t.id === activeId.value)?.text ?? '')
function go(id: string) {
  navOpen.value = false
  router.push({ hash: '#' + id })
}
const router = useRouter()

function onClick(e: MouseEvent) {
  const t = e.target as HTMLElement
  if (t.classList.contains('copy-btn')) {
    const code = t.parentElement?.querySelector('code')?.textContent ?? ''
    void navigator.clipboard?.writeText(code)
    t.textContent = 'Copied'
    setTimeout(() => (t.textContent = 'Copy'), 1200)
  }
}
</script>

<template>
  <div class="min-h-screen">
    <NavBar />
    <div id="main" tabindex="-1" class="mx-auto max-w-6xl px-4 py-4 md:flex md:gap-8 md:py-6">
      <aside class="shrink-0 md:sticky md:top-4 md:max-h-[calc(100vh-2rem)] md:w-64 lg:w-80 md:self-start md:overflow-y-auto">
        <nav aria-label="Documents" class="mb-3 flex flex-wrap border-b border-pico-border text-sm">
          <RouterLink
            v-for="(d, k) in DOCS"
            :key="k"
            :to="`/guide/${k}`"
            class="-mb-px flex-1 whitespace-nowrap border-b-2 px-2.5 py-2 text-center md:flex-none"
            :class="doc === k ? 'border-accent-light font-semibold text-white' : 'border-transparent text-zinc-300 hover:text-white'"
            :aria-current="doc === k ? 'page' : undefined"
          >{{ d.title }}</RouterLink>
        </nav>

        <!-- Mobile: sticky Contents bar that follows the reader down a very long page -->
        <div class="sticky top-0 z-30 -mx-4 border-b border-pico-border bg-pico-bg/95 px-4 py-2 backdrop-blur-sm md:hidden">
          <button type="button" class="btn-secondary btn-sm w-full justify-between" :aria-expanded="navOpen" aria-controls="toc" @click="navOpen = !navOpen">
            <span class="shrink-0">Contents</span>
            <span class="min-w-0 truncate font-normal text-zinc-400">{{ activeText }}</span>
            <span aria-hidden="true">{{ navOpen ? '&#9650;' : '&#9660;' }}</span>
          </button>
          <nav v-show="navOpen" id="toc" aria-label="Contents (mobile)" class="absolute inset-x-0 top-full max-h-[60vh] space-y-0.5 overflow-y-auto border-b border-pico-border bg-pico-panel px-4 py-2 text-sm shadow-xl">
            <a
              v-for="t in rendered.toc"
              :key="t.id"
              :href="`#${t.id}`"
              class="block rounded-sm px-2 py-2"
              :class="[t.level === 3 ? 'pl-6 text-xs' : '', activeId === t.id ? 'bg-pico-card font-medium text-white' : 'text-zinc-300']"
              @click.prevent="go(t.id)"
            >{{ t.text }}</a>
          </nav>
        </div>

        <nav aria-label="Contents" class="hidden space-y-0.5 text-sm md:block">
          <a
            v-for="t in rendered.toc"
            :key="t.id"
            :href="`#${t.id}`"
            class="block rounded-sm border-l-2 px-2 py-1 leading-snug hover:text-white"
            :class="[t.level === 3 ? 'ml-3 text-xs' : '', activeId === t.id ? 'border-accent-light bg-pico-card/60 font-medium text-white' : 'border-transparent text-zinc-300']"
            :aria-current="activeId === t.id ? 'location' : undefined"
            @click.prevent="go(t.id)"
          >{{ t.text }}</a>
        </nav>
      </aside>
      <div v-if="doc === 'admin' && (adminLoading || adminError || !adminMd)" class="mt-4 min-w-0 flex-1 md:mt-0">
        <p v-if="adminLoading" class="help" role="status">Loading admin guide...</p>
        <div v-else-if="adminError" role="alert">
          <p class="text-red-400">{{ adminError }}</p>
          <button type="button" class="btn-secondary btn-sm mt-2" @click="loadAdmin">Retry</button>
        </div>
      </div>
      <article v-else class="doc mt-4 min-w-0 flex-1 md:mt-0" @click="onClick" v-html="rendered.html" />
    </div>
  </div>
</template>

<style>
.doc { font-size: 15px; line-height: 1.65; color: #d4d4d8; overflow-wrap: anywhere; }
.doc h1 { font-size: 1.75rem; margin: 0 0 1rem; color: #fff; font-weight: 700; }
.doc h2 { font-size: 1.35rem; margin: 2.25rem 0 .75rem; padding-bottom: .3rem; border-bottom: 1px solid #2c2c3c; color: #fff; font-weight: 600; }
.doc h3 { font-size: 1.1rem; margin: 1.5rem 0 .5rem; color: #fff; font-weight: 600; }
.doc h2, .doc h3 { scroll-margin-top: 64px; }
@media (min-width: 768px) { .doc h2, .doc h3 { scroll-margin-top: 8px; } }
.doc .anchor { margin-left: -1.1em; padding-right: .4em; color: #4a4a5e; opacity: 0; text-decoration: none; }
.doc h2:hover .anchor, .doc h3:hover .anchor { opacity: 1; }
.doc p, .doc ul, .doc ol { margin: .6rem 0; }
.doc ul { list-style: disc; padding-left: 1.4rem; }
.doc ol { list-style: decimal; padding-left: 1.4rem; }
.doc a { color: #7aa0d4; }
.doc code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .875em; background: #1c1c28; border-radius: 4px; padding: .1em .35em; overflow-wrap: anywhere; }
.doc .code { position: relative; margin: .8rem 0; }
.doc .code pre { overflow-x: auto; background: #16161f; border: 1px solid #2c2c3c; border-radius: 6px; padding: .8rem 1rem; }
.doc .code pre code { background: none; padding: 0; white-space: pre; overflow-wrap: normal; font-size: 13px; }
.doc .copy-btn { position: absolute; top: 6px; right: 6px; font-size: 11px; color: #a1a1aa; background: #1c1c28; border: 1px solid #2c2c3c; border-radius: 4px; padding: 1px 8px; }
.doc .copy-btn:hover { color: #fff; }
.doc .tbl-wrap { overflow-x: auto; margin: .8rem 0; }
.doc table { border-collapse: collapse; font-size: 14px; width: 100%; background: #16161f; border: 1px solid #2c2c3c; }
.doc th, .doc td { padding: 5px 10px; border: 1px solid #2c2c3c; text-align: left; vertical-align: middle; overflow-wrap: normal; word-break: normal; }
.doc th { white-space: nowrap; }
.doc td code { overflow-wrap: normal; white-space: nowrap; font-size: 12px; font-variant-numeric: tabular-nums; }
.doc .tbl-wrap { overflow-x: auto; }
.doc th { background: #1c1c28; color: #fff; }
.doc blockquote { border-left: 3px solid #2c2c3c; padding-left: 1rem; color: #a1a1aa; margin: .8rem 0; }
</style>
