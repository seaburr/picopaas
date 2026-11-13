import { expect, test, type Page, type Route } from '@playwright/test'
import { login, spillers } from './helpers'

// Build page at each build state, API stubbed. Screenshots go to SHOTS_DIR when set.
const ID = 97
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
    id: ID, name: 'UX stub', status: 'done', app_id: null, app_hostname: null, app_status: null, file_count: 0, cost_usd: 0.1, last_run_at: null, last_error: null,
    open_questions: 0, created_by: { id: 1, email: 'e2e-admin@example.com' }, created_at: '2026-11-05T09:00:00Z', brief: 'Build me a small page.', project_id: 1, slug: 'ux-stub',
    files: [], runs, inbox: [], spend: { build_usd: 0.1, cap_build: 5, month_usd: 0.1, last30_usd: 0.1 },
    output: { mode: 'download', repo: null, branch: null, push_on_finish: false, open_pr: false, auth: 'project', token: { set: false }, project_token_set: false, can_push: false },
    can_run: true, ...extra,
  }
}
const uploadsOk = { uploads: [], quota: {}, notice: '', uploads_enabled: true, uploads_project_enabled: true, uploads_platform_enabled: true, uploads_disabled_reason: null, uploads_not_enabled_label: '', uploads_notice: '' }
const summaryOk = { count: 0, asset_count: 0, reference_count: 0, asset_bytes: 0, ref_bytes: 0, max_files: 20, asset_limit_bytes: 1e6, ref_limit_bytes: 1e6, changed_since_deploy: false, ...uploadsOk }
async function stub(page: Page, b: Json) {
  await page.route(new RegExp(`/kaisen/builds/${ID}(/|\\?|$)`), async (route: Route) => {
    const req = route.request()
    if (req.resourceType() !== 'fetch' && req.resourceType() !== 'xhr') return route.continue()
    const path = new URL(req.url()).pathname.replace(`/kaisen/builds/${ID}`, '')
    if (req.method() === 'GET' && path === '') return route.fulfill({ json: { ...b, uploads: summaryOk } })
    if (req.method() === 'GET' && path === '/uploads') return route.fulfill({ json: uploadsOk })
    if (req.method() === 'GET') return route.fulfill({ json: [] })
    return route.fulfill({ json: { ...b, uploads: summaryOk } })
  })
  await page.route('**/projects/*/kaisen', (r) => r.fulfill({ status: 404, json: { detail: 'none' } }))
}
const q = { id: 3, kind: 'question', status: 'open', text: 'Should the page use a dark or a light theme?', answer: null, created_at: '2026-11-05T10:01:10Z' }
const long = 'x'.repeat(160) + ' const a = veryLongIdentifierWithoutAnySpacesThatShouldWrapInsideTheBubbleAndNotSpill()'
const states: Record<string, Json> = {
  queued: build([], { status: 'queued' }),
  running: build([run(1), run(2, { status: 'running', summary: null, finished_at: null, step: 4, last_tool: 'write_file', cost_usd: 0.42 })], { status: 'running' }),
  waiting: build([run(1, { summary: null }), run(2)], { status: 'waiting', inbox: [q], open_questions: 1 }),
  failed: build([run(1, { status: 'error', summary: null, error: 'The person who started this run was suspended.' })], { status: 'error' }),
  stopped: build([run(1, { status: 'stopped', summary: null, error: 'Stopped by you.' })], { status: 'stopped' }),
  cap: build([run(1, { status: 'error', summary: 'Partial.', error: 'Build spend cap reached.' })], { status: 'error', inbox: [{ id: 4, kind: 'budget', status: 'open', text: 'Spend cap reached: $5.00', answer: null, created_at: '2026-11-05T10:02:00Z' }], spend: { build_usd: 5, cap_build: 5, month_usd: 5, last30_usd: 5 } }),
  long: build([run(1, { summary: long }), run(2, { kind: 'feedback', request: long })]),
  deployed: build([run(1), run(2)], { app_id: 777, app_hostname: 'a.picopaas.xyz', app_status: 'running', file_count: 1, files: [{ path: 'a.txt', size: 1 }] }),
}
const sizes = [[1440, 900], [1024, 768], [768, 900], [390, 844]]
for (const [name, b] of Object.entries(states)) {
  test(`state ${name}`, async ({ page }) => {
    await login(page)
    await stub(page, b)
    await page.route('**/apps/777', (r) => r.fulfill({ json: { id: 777, name: 'a', hostname: 'a.picopaas.xyz', status: 'running', status_detail: null, env: [] } }))
    for (const [w, h] of sizes) {
      await page.setViewportSize({ width: w, height: h })
      await page.goto(`/kaisen/${ID}`)
      await expect(page.getByRole('heading', { name: 'UX stub' })).toBeVisible()
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}-${w}.png` })
      expect(await spillers(page)).toEqual([])
    }
    if (SHOTS) {
      await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'))
      await page.setViewportSize({ width: 1280, height: 800 })
      await page.screenshot({ path: `${SHOTS}/${name}-light.png` })
    }
  })
}

test('Stop run: shown only while a question is waiting, posts to the stop route', async ({ page }) => {
  await login(page)
  const waitingB = build([run(1, { summary: null }), run(2, { status: 'waiting', summary: null, finished_at: null })], { status: 'waiting', inbox: [q], open_questions: 1 })
  const stoppedB = build([run(1, { summary: null }), run(2, { status: 'stopped', summary: null, error: 'Stopped by e2e-admin@example.com while waiting for an answer.' })], { status: 'stopped' })
  await stub(page, waitingB)
  await page.route(new RegExp(`/kaisen/builds/${ID}/snapshots$`), (r) => r.fulfill({ json: [{ id: 5, run_id: 2, run_number: 2, kind: 'run_start', created_at: '2026-11-05T10:01:00Z', files_count: 1, bytes: 10 }] }))
  let posted = ''
  await page.route(new RegExp(`/kaisen/builds/${ID}/runs/2/stop$`), (r) => {
    posted = r.request().method() + ' ' + new URL(r.request().url()).pathname
    return r.fulfill({ json: { ...stoppedB, uploads: summaryOk } })
  })
  await page.goto(`/kaisen/${ID}`)
  await page.getByText('Restore points', { exact: true }).click()
  await expect(page.getByText('Answer the question or stop the run, then restore.')).toBeVisible()
  await page.getByRole('button', { name: 'Stop run' }).click()
  const dlg = page.getByRole('dialog')
  await expect(dlg.getByRole('heading', { name: 'Stop this run?' })).toBeVisible()
  await dlg.getByRole('button', { name: 'Keep waiting' }).click()
  await expect(dlg).toBeHidden()
  await page.getByRole('button', { name: 'Stop run' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Stop run' }).click()
  await expect(page.getByText('Stopped by e2e-admin@example.com while waiting for an answer.').first()).toBeVisible()
  expect(posted).toBe(`POST /kaisen/builds/${ID}/runs/2/stop`)
  await expect(page.getByRole('button', { name: 'Stop run' })).toHaveCount(0)
})

for (const name of ['running', 'deployed']) {
  test(`Stop run absent in ${name} state`, async ({ page }) => {
    await login(page)
    await stub(page, states[name])
    await page.route('**/apps/777', (r) => r.fulfill({ json: { id: 777, name: 'a', hostname: 'a.picopaas.xyz', status: 'running', status_detail: null, env: [] } }))
    await page.goto(`/kaisen/${ID}`)
    await expect(page.getByRole('heading', { name: 'UX stub' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Stop run' })).toHaveCount(0)
  })
}

test('cap-hit build offers exactly one Resume, in the conversation by the run', async ({ page }) => {
  await login(page)
  await stub(page, states.cap)
  await page.goto(`/kaisen/${ID}`)
  await expect(page.getByRole('heading', { name: 'UX stub' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Resume' })).toHaveCount(1)
  await expect(page.getByText('press Resume on the run below')).toBeVisible()
  await expect(page.getByText('Use Resume in the conversation')).toBeVisible()
})

for (const reason of ['starter_gone', 'project_suspended']) {
  test(`no Resume when the run failed with ${reason}`, async ({ page }) => {
    await login(page)
    await stub(page, build([run(1, { status: 'error', summary: null, error: 'Cannot continue.', error_reason: reason })], { status: 'error' }))
    await page.goto(`/kaisen/${ID}`)
    await expect(page.getByText('Cannot continue.')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Resume' })).toHaveCount(0)
    await expect(page.getByText('Use Resume in the conversation')).toHaveCount(0)
  })
}
