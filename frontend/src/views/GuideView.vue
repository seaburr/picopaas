<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import DOMPurify from 'dompurify'
import { Marked } from 'marked'
import userMd from '@docs/USER_GUIDE.md?raw'
import apiMd from '@docs/API.md?raw'
import cliMd from '@docs/CLI.md?raw'
import NavBar from '../components/NavBar.vue'

const DOCS = {
  user: { title: 'User guide', md: userMd },
  api: { title: 'API', md: apiMd },
  cli: { title: 'CLI', md: cliMd },
} as const
type DocKey = keyof typeof DOCS

const route = useRoute()
const doc = computed<DocKey>(() => (route.params.doc as DocKey) || 'user')
const navOpen = ref(false)

const slug = (t: string) =>
  t.toLowerCase().replace(/[`*_]/g, '').replace(/[^a-z0-9 -]/g, '').trim().replace(/[ -]+/g, '-')
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const LINKS: Record<string, string> = {
  'USER_GUIDE.md': '/guide/user',
  'API.md': '/guide/api',
  'CLI.md': '/guide/cli',
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
        return `<div class="tbl"><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>`
      },
      link({ href, tokens }) {
        const [file, hash] = href.split('#')
        const target = LINKS[file.replace(/^(\.\/|\.\.\/|docs\/)+/, '')]
        const h = target ? target + (hash ? '#' + hash : '') : href
        return `<a href="${esc(h)}">${this.parser.parseInline(tokens)}</a>`
      },
    },
  })
  const html = DOMPurify.sanitize(m.parse(DOCS[doc.value].md, { async: false }) as string)
  return { html, toc }
})

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
    <div class="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-6 md:flex-row">
      <aside class="shrink-0 md:sticky md:top-4 md:max-h-[calc(100vh-2rem)] md:w-64 md:self-start md:overflow-y-auto">
        <div class="mb-3 flex gap-2 text-sm">
          <RouterLink
            v-for="(d, k) in DOCS"
            :key="k"
            :to="`/guide/${k}`"
            class="rounded border border-pico-border px-2 py-1 text-zinc-300 hover:bg-pico-card"
            :class="{ 'bg-pico-card text-white': doc === k }"
          >{{ d.title }}</RouterLink>
        </div>
        <button type="button" class="mb-2 text-xs text-zinc-400 md:hidden" @click="navOpen = !navOpen">
          {{ navOpen ? 'Hide' : 'Show' }} contents
        </button>
        <nav :class="[navOpen ? 'block' : 'hidden', 'space-y-0.5 text-sm md:block']">
          <a
            v-for="t in rendered.toc"
            :key="t.id"
            :href="`#${t.id}`"
            class="block truncate text-zinc-400 hover:text-white"
            :class="t.level === 3 ? 'pl-4 text-xs' : ''"
            @click.prevent="$router.push({ hash: '#' + t.id }); navOpen = false"
          >{{ t.text }}</a>
        </nav>
      </aside>
      <article class="doc min-w-0 flex-1" @click="onClick" v-html="rendered.html" />
    </div>
  </div>
</template>

<style>
.doc { font-size: 15px; line-height: 1.65; color: #d4d4d8; overflow-wrap: anywhere; }
.doc h1 { font-size: 1.75rem; margin: 0 0 1rem; color: #fff; font-weight: 700; }
.doc h2 { font-size: 1.35rem; margin: 2.25rem 0 .75rem; padding-bottom: .3rem; border-bottom: 1px solid #2c2c3c; color: #fff; font-weight: 600; }
.doc h3 { font-size: 1.1rem; margin: 1.5rem 0 .5rem; color: #fff; font-weight: 600; }
.doc h2, .doc h3 { scroll-margin-top: 8px; }
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
.doc .tbl { overflow-x: auto; margin: .8rem 0; }
.doc table { border-collapse: collapse; font-size: 14px; width: 100%; background: #16161f; border: 1px solid #2c2c3c; }
.doc th, .doc td { padding: 6px 12px; border: 1px solid #2c2c3c; text-align: left; vertical-align: top; }
.doc th { background: #1c1c28; color: #fff; }
.doc blockquote { border-left: 3px solid #2c2c3c; padding-left: 1rem; color: #a1a1aa; margin: .8rem 0; }
</style>
