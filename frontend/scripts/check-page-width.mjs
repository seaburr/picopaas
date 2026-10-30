// Fails when a view's <main> sets its own width instead of the shared page classes (src/style.css).
// Signed-in pages use `page-main` so every page lines up; only the small centered sign-in/status pages
// listed in NARROW may use `page-narrow`. Run: npm run check:layout (also in CI).
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const VIEWS = new URL('../src/views/', import.meta.url).pathname
const NARROW = new Set(['InviteView.vue', 'NotFoundView.vue', 'LoginView.vue', 'RegisterView.vue', 'AccountStatusView.vue'])

const problems = []
for (const file of readdirSync(VIEWS).filter((f) => f.endsWith('.vue')).sort()) {
  const src = readFileSync(join(VIEWS, file), 'utf8')
  for (const m of src.matchAll(/<main\b[^>]*>/g)) {
    const tag = m[0]
    const line = src.slice(0, m.index).split('\n').length
    const where = `src/views/${file}:${line}`
    if (/:class=/.test(tag)) problems.push(`${where}: <main> must use a static class, not :class`)
    if (/\bmax-w-/.test(tag)) problems.push(`${where}: <main> sets max-w-*; use page-main instead`)
    const main = /\bpage-main\b/.test(tag)
    const narrow = /\bpage-narrow\b/.test(tag)
    if (!main && !narrow) problems.push(`${where}: <main> needs class page-main`)
    if (narrow && !NARROW.has(file)) problems.push(`${where}: page-narrow is only for sign-in/status pages; use page-main`)
  }
}

if (problems.length) {
  console.error('Page width check failed (see frontend/src/style.css page-main):\n' + problems.map((p) => '  ' + p).join('\n'))
  process.exit(1)
}
console.log('Page width check passed.')
