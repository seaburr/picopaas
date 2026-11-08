import { expect, test, type Page, type Route } from '@playwright/test'
import { login } from './helpers'

// Kaisen build page, API stubbed: typing indicator, user bubble, feedback request text, updating wording.
const ID = 98
type Json = Record<string, unknown>
const SHOTS = process.env.SHOTS_DIR

function run(n: number, extra: Json = {}): Json {
  return {
    id: n, kind: n === 1 ? 'build' : 'feedback', status: 'done', summary: `Run ${n} finished.`, error: null, cost_usd: 0.01,
    started_at: `2026-11-05T10:0${n}:00Z`, finished_at: `2026-11-05T10:0${n}:30Z`, transcript: [{ role: 'assistant', text: 'Done.' }], transcript_trimmed: false, ...extra,
  }
}
function build(runs: Json[], extra: Json = {}): Json {
  return {
    id: ID, name: 'Typing stub', status: 'done', app_id: null, app_hostname: null, app_status: null, file_count: 0, cost_usd: 0.1, last_run_at: null, last_error: null,
    open_questions: 0, created_by: { id: 1, email: 'e2e-admin@example.com' }, created_at: '2026-11-05T09:00:00Z', brief: 'Build me a small page.', project_id: 1,
    slug: 'typing-stub', files: [], runs, inbox: [], spend: { build_usd: 0.1, cap_build: 5, month_usd: 0.1, last30_usd: 0.1 },
    output: { mode: 'download', repo: null, branch: null, push_on_finish: false, open_pr: false, auth: 'project', token: { set: false }, project_token_set: false, can_push: false },
    can_run: true, ...extra,
  }
}
const uploadsOk = { uploads: [], quota: {}, notice: '', uploads_enabled: true, uploads_project_enabled: true, uploads_platform_enabled: true, uploads_disabled_reason: null, uploads_not_enabled_label: '', uploads_notice: '' }
const summaryOk = { count: 0, asset_count: 0, reference_count: 0, asset_bytes: 0, ref_bytes: 0, max_files: 20, asset_limit_bytes: 1e6, ref_limit_bytes: 1e6, changed_since_deploy: false, ...uploadsOk }

async function stub(page: Page, b: Json, appObj?: Json) {
  await page.route(new RegExp(`/kaisen/builds/${ID}(/|\\?|$)`), async (route: Route) => {
    const req = route.request()
    if (req.resourceType() !== 'fetch' && req.resourceType() !== 'xhr') return route.continue()
    const path = new URL(req.url()).pathname.replace(`/kaisen/builds/${ID}`, '')
    if (req.method() === 'GET' && path === '') return route.fulfill({ json: { ...b, uploads: summaryOk } })
    if (req.method() === 'GET' && path === '/uploads') return route.fulfill({ json: uploadsOk })
    return route.fulfill({ json: [] })
  })
  if (appObj) await page.route(/\/apps\/777(\?|$)/, (r) => (r.request().resourceType() === 'fetch' || r.request().resourceType() === 'xhr' ? r.fulfill({ json: appObj }) : r.continue()))
  await page.route('**/projects/*/kaisen', (r) => r.fulfill({ status: 404, json: { detail: 'none' } }))
}
const shot = async (page: Page, name: string) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` }) }
// The auth store applies the server's theme after login; flip the attribute on the loaded page (CSS only).
const theme = (page: Page, t: 'dark' | 'light') => page.evaluate((v) => document.documentElement.setAttribute('data-theme', v), t)

const active = (extra: Json = {}) => build([run(1), run(2, { kind: 'feedback', request: 'Make the heading bigger and blue, please.' }), run(3, { kind: 'feedback', status: 'running', summary: null, finished_at: null, request: 'Add a contact form.' })], { status: 'running', ...extra })

test('typing indicator shows while a run is active, with the sent text and a filled bubble', async ({ page }) => {
  await login(page)
  await stub(page, active())
  await page.goto(`/kaisen/${ID}`)
  const typing = page.getByTestId('typing')
  await expect(typing).toBeVisible()
  await expect(typing.locator('.typing-dots i')).toHaveCount(3)
  await expect(typing.getByText('Kaisen is working')).toBeAttached()
  await expect(page.getByText('Make the heading bigger and blue, please.')).toBeVisible()
  await expect(page.getByText('Add a contact form.')).toBeVisible()
  await expect(page.getByText('Sent a change.')).toHaveCount(0)
  const bubble = page.locator('.bubble-you', { hasText: 'Make the heading bigger' })
  const bg = await bubble.evaluate((el) => getComputedStyle(el).backgroundColor)
  expect(bg).not.toMatch(/rgba\(0, 0, 0, 0\)|transparent/)
  expect(bg).not.toBe('rgb(18, 20, 23)') // not the page background
  await shot(page, 'chat-active-1440-dark')
  await page.setViewportSize({ width: 390, height: 800 })
  await shot(page, 'chat-active-390-dark')
})

test('light mode keeps a clear bubble', async ({ page }) => {
  await login(page)
  await stub(page, active())
  await page.goto(`/kaisen/${ID}`)
  await expect(page.getByTestId('typing')).toBeVisible()
  await theme(page, 'light')
  const bg = await page.locator('.bubble-you').last().evaluate((el) => getComputedStyle(el).backgroundColor)
  expect(bg).not.toBe('rgb(241, 238, 230)')
  await shot(page, 'chat-active-1440-light')
})

test('a feedback run without any text says so plainly', async ({ page }) => {
  await login(page)
  await stub(page, build([run(1), run(2, { request: null })]))
  await page.goto(`/kaisen/${ID}`)
  await expect(page.getByText('Your change (text not available)')).toBeVisible()
})

const appObj = (extra: Json) => ({ id: 777, name: 'a', hostname: 'a.picopaas.xyz', status: 'running', status_detail: null, env: [], ...extra })
const live = { app_id: 777, app_hostname: 'a.picopaas.xyz', file_count: 1, files: [{ path: 'a.txt', size: 1 }] }
const deployInbox = [{ id: 5, kind: 'deploy', status: 'dismissed', text: 'Preview deployed: https://a.picopaas.xyz\nhttp: 200', created_at: '2026-11-05T10:05:00Z', answer: null }]

test('updating app: says the current version stays live, no "Starting", no "Deployed"', async ({ page }) => {
  await login(page)
  await stub(page, build([run(1), run(2)], { ...live, app_status: 'pending', inbox: deployInbox }), appObj({ status: 'pending', updating: true }))
  await page.goto(`/kaisen/${ID}`)
  await expect(page.getByText('Updating your app. The current version stays live until the new one is ready.')).toBeVisible()
  await expect(page.getByText('Starting your app')).toHaveCount(0)
  await expect(page.getByText('Deployed', { exact: true })).toHaveCount(0)
  await shot(page, 'updating-1440-dark')
  await page.setViewportSize({ width: 390, height: 900 })
  await page.getByText('Build details').click()
  await shot(page, 'updating-390-dark')
})

test('first deploy says Starting; running and not updating says Deployed', async ({ page }) => {
  await login(page)
  await stub(page, build([run(1)], { ...live, app_status: 'deploying', inbox: deployInbox }), appObj({ status: 'deploying' }))
  await page.goto(`/kaisen/${ID}`)
  await expect(page.getByText('Starting your app.')).toBeVisible()
  await page.unrouteAll()
  await stub(page, build([run(1)], { ...live, app_status: 'running', inbox: deployInbox }), appObj({ status: 'running', updating: false }))
  await page.reload()
  await expect(page.getByText('Deployed', { exact: true })).toBeVisible()
  await expect(page.getByText('Starting your app')).toHaveCount(0)
})
