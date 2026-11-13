import { expect, test, type Page } from '@playwright/test'
import { login } from './helpers'

// Reviewer D: the build page side panels and the Kaisen settings page, with stubbed API responses.
const SHOTS = process.env.SHOTS_DIR ?? ''
const ID = 98
const uploadsOk = { uploads: [], quota: {}, notice: '', uploads_enabled: true, uploads_project_enabled: true, uploads_platform_enabled: true, uploads_disabled_reason: null, uploads_not_enabled_label: '', uploads_notice: '' }
const summaryOk = { count: 0, asset_count: 0, reference_count: 0, asset_bytes: 0, ref_bytes: 0, max_files: 20, asset_limit_bytes: 1e6, ref_limit_bytes: 1e6, changed_since_deploy: false, ...uploadsOk }
const LONG = 'src/components/features/dashboard/widgets/very-long-directory-name-for-the-widget/SomeExtremelyLongComponentFileNameForTesting.test.tsx'
const files = [{ path: 'main.py', size: 1200 }, { path: 'templates/index.html', size: 4300 }, { path: LONG, size: 90 }]
const snap = (id: number, kind: string, n: number) => ({ id, run_id: id, run_number: n, kind, created_at: '2026-11-12T09:00:00Z', files_count: 3, bytes: 5600 })
function build(): Record<string, unknown> {
  return {
    id: ID, name: 'Panels stub', status: 'done', app_id: 7, app_hostname: 'stub-app.picopaas.xyz', app_status: 'running', file_count: 3, cost_usd: 0.1,
    last_run_at: null, last_error: null, open_questions: 0, created_by: { id: 1, email: 'e2e-admin@example.com' }, created_at: '2026-11-05T09:00:00Z',
    brief: 'x', project_id: 1, slug: 'panels-stub', files, runs: [], snapshots_kept: 10,
    inbox: [{ id: 3, kind: 'deploy', text: 'Preview deployed: ok\nhttp: 200', status: 'open', answer: null, created_at: '2026-11-12T09:00:00Z' }],
    secrets: [{ name: 'STRIPE_API_KEY', value: 'sk-...wxyz', updated_at: '2026-11-12T09:00:00Z' }],
    spend: { build_usd: 0.1, cap_build: 5, month_usd: 0.1, last30_usd: 0.1 },
    output: { mode: 'download', repo: null, branch: null, push_on_finish: false, open_pr: false, auth: 'project', token: { set: false }, project_token_set: false, can_push: false },
    can_run: true, uploads: summaryOk,
  }
}
const diff = { from: 'a', to: 'b', added: ['new.py'], removed: ['old.py'], changed: [{ path: LONG, diff: '@@ -1,2 +1,2 @@\n-a\n+b' }, { path: 'big.py', diff: '' }], truncated: true }
async function stub(page: Page) {
  await page.route(new RegExp(`/kaisen/builds/${ID}(/|\\?|$)`), (route) => {
    const req = route.request()
    if (req.resourceType() !== 'fetch' && req.resourceType() !== 'xhr') return route.continue()
    const path = new URL(req.url()).pathname.replace(`/kaisen/builds/${ID}`, '')
    if (req.method() === 'GET' && path === '') return route.fulfill({ json: build() })
    if (path === '/uploads') return route.fulfill({ json: uploadsOk })
    if (path === '/snapshots') return route.fulfill({ json: [snap(2, 'run_start', 2), snap(1, 'before_restore', 1)] })
    if (path.startsWith('/snapshots/')) return route.fulfill({ json: diff })
    if (path.startsWith('/files/')) return route.fulfill({ json: { content: 'print(1)\n', binary: false, truncated: false } })
    return route.fulfill({ json: [] })
  })
  await page.route('**/projects/*/kaisen', (r) => r.fulfill({ status: 404, json: { detail: 'none' } }))
  await page.route('**/apps/7', (r) => r.fulfill({ json: { id: 7, name: 'a', status: 'running', hostname: 'stub-app.picopaas.xyz', updating: false } }))
  await page.route('https://stub-app.picopaas.xyz/**', (r) => r.fulfill({ contentType: 'text/html', body: '<p>hi</p>' }))
}
const settings = (edit: boolean) => ({
  anthropic_key: { set: true, last4: 'wxyz' }, model: 'claude-haiku', effort: 'low', models: ['claude-haiku', 'claude-sonnet'], efforts: ['low', 'high'],
  reviewer: 'off', reviewer_model: 'claude-haiku', package_steward: 'on', cap_run: 1, cap_day: 5, cap_build: 3, github_token: { set: false, last4: null },
  web_access: false, web_project_enabled: true, web_allowed_domains: [], web_supported: true, web_models: [], web_note: '', web_search_usd: 0.01, web_domains_max: 20,
  can_edit: edit, updated_at: null,
})
async function stubSettings(page: Page, edit: boolean) {
  await page.route('**/projects/*/kaisen', (r) => r.fulfill({ json: settings(edit) }))
  await page.route('**/projects/*/kaisen/members', (r) => r.fulfill({ json: { project_id: 1, can_edit: edit, members: [{ user_id: 1, email: 'someone-with-a-rather-long-address@example.com', role: 'owner', cap_day: 5, cap_build: null, spend_today_usd: 0.42 }] } }))
  await page.route('**/kaisen/spend**', (r) => r.fulfill({ json: { last30_usd: 1.5 } }))
}
const shot = async (page: Page, name: string) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true }) }

for (const w of [1440, 1024, 390]) {
  test(`build panels at ${w}`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: 1000 })
    await login(page)
    await stub(page)
    await page.goto(`/kaisen/${ID}`)
    await expect(page.locator('#h-files')).toBeAttached()
    await page.evaluate(() => document.querySelectorAll('details').forEach((d) => { d.open = true }))
    await page.getByRole('button', { name: /View changes since/ }).first().click()
    await expect(page.getByText('too large').first()).toBeVisible()
    for (const id of ['h-secrets', 'h-restore', 'h-files']) if (SHOTS) await page.locator(`details:has(> summary #${id})`).screenshot({ path: `${SHOTS}/${id}-${w}.png` })
    await page.getByRole('button', { name: 'Read project files' }).click()
    await shot(page, `reader-${w}`)
    await page.keyboard.press('Escape')
  })
  test(`settings at ${w}`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: 1000 })
    await login(page)
    await stubSettings(page, true)
    await page.goto('/projects/1/kaisen/settings')
    await expect(page.getByRole('heading', { name: 'Anthropic' })).toBeVisible()
    await shot(page, `settings-${w}`)
  })
}
test('settings read-only member', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  await login(page)
  await stubSettings(page, false)
  await page.goto('/projects/1/kaisen/settings')
  await expect(page.getByRole('heading', { name: 'Anthropic' })).toBeVisible()
  await shot(page, 'settings-readonly')
})

test('secret delete asks first', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  await login(page)
  await stub(page)
  await page.goto(`/kaisen/${ID}`)
  await expect(page.locator('#h-secrets')).toBeAttached()
  await page.evaluate(() => document.querySelectorAll('details').forEach((d) => { d.open = true }))
  await page.getByRole('button', { name: 'Delete STRIPE_API_KEY' }).click()
  await expect(page.getByRole('dialog', { name: 'Delete secret' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: 'Delete secret' })).toHaveCount(0)
})
