import { expect, test, type Page, type Route } from '@playwright/test'
import { login, spillers } from './helpers'

// Fork a build (contract: POST /kaisen/builds/{id}/fork), fork notice, home meta line. API stubbed.
const SRC = 97
const NEW = 98
const SHOTS = process.env.SHOTS_DIR
type Json = Record<string, unknown>
const sizes = [[1440, 900], [768, 900], [390, 844]]

const run = (n: number, extra: Json = {}): Json => ({
  id: n, kind: n === 1 ? 'build' : 'feedback', status: 'done', summary: `Run ${n} finished.`, error: null, cost_usd: 0.01,
  started_at: `2026-11-05T10:0${n}:00Z`, finished_at: `2026-11-05T10:0${n}:30Z`, transcript: [{ role: 'assistant', text: 'Done.' }], transcript_trimmed: false, ...extra,
})
function build(id: number, name: string, extra: Json = {}): Json {
  return {
    id, name, status: 'done', app_id: null, app_hostname: null, app_status: null, file_count: 0, cost_usd: 0.1, last_run_at: null, last_error: null,
    open_questions: 0, created_by: { id: 1, email: 'e2e-admin@example.com' }, created_at: '2026-11-05T09:00:00Z', brief: 'Build me a small page.', project_id: 1, slug: `s${id}`,
    files: [], runs: [run(1), run(2)], inbox: [], spend: { build_usd: 0.1, cap_build: 5, month_usd: 0.1, last30_usd: 0.1 },
    output: { mode: 'download', repo: null, branch: null, push_on_finish: false, open_pr: false, auth: 'project', token: { set: false }, project_token_set: false, can_push: false },
    can_run: true, snapshots_kept: 10, forked_from: null, ...extra,
  }
}
const snap = (id: number, kind: string, n: number) => ({ id, run_id: n, run_number: n, kind, created_at: `2026-11-05T10:0${n}:00Z`, files_count: 2, bytes: 100 })
const uploadsOk = { uploads: [], quota: {}, notice: '', uploads_enabled: true, uploads_project_enabled: true, uploads_platform_enabled: true, uploads_disabled_reason: null, uploads_not_enabled_label: '', uploads_notice: '' }
const summaryOk = { count: 0, asset_count: 0, reference_count: 0, asset_bytes: 0, ref_bytes: 0, max_files: 20, asset_limit_bytes: 1e6, ref_limit_bytes: 1e6, changed_since_deploy: false, ...uploadsOk }

interface Opts { src?: Json; fork?: Json; forkRes?: { status: number; json: Json }; posts?: { url: string; body: unknown }[] }
async function stub(page: Page, o: Opts = {}) {
  const src = o.src ?? build(SRC, 'UX stub')
  const fork = o.fork ?? build(NEW, 'UX stub fork', { runs: [], status: 'idle', forked_from: { id: SRC, name: 'UX stub', snapshot_id: null, secret_names: [] } })
  await page.route(/\/kaisen\/builds\/\d+(\/|\?|$)/, async (route: Route) => {
    const req = route.request()
    if (req.resourceType() !== 'fetch' && req.resourceType() !== 'xhr') return route.continue()
    const path = new URL(req.url()).pathname.replace('/kaisen/builds/', '')
    const [id, ...rest] = path.split('/')
    const sub = rest.join('/')
    const b = Number(id) === SRC ? src : fork
    if (req.method() === 'POST' && sub === 'fork') {
      o.posts?.push({ url: path, body: JSON.parse(req.postData() ?? 'null') })
      return route.fulfill(o.forkRes ?? { status: 201, json: { ...fork, uploads: summaryOk } })
    }
    if (req.method() === 'GET' && sub === '') return route.fulfill({ json: { ...b, uploads: summaryOk } })
    if (req.method() === 'GET' && sub === 'uploads') return route.fulfill({ json: uploadsOk })
    if (req.method() === 'GET' && sub === 'snapshots') return route.fulfill({ json: Number(id) === SRC ? [snap(2, 'run_start', 2), snap(1, 'before_restore', 1)] : [] })
    return route.fulfill({ json: [] })
  })
  await page.route('**/projects/*/kaisen', (r) => r.fulfill({ status: 404, json: { detail: 'none' } }))
}
const dialog = (page: Page) => page.getByRole('dialog', { name: 'Fork this build' })

test('header Fork opens the dialog on Current files; body is exact; navigates to the fork', async ({ page }) => {
  const posts: { url: string; body: unknown }[] = []
  await login(page)
  await stub(page, { posts })
  await page.goto(`/kaisen/${SRC}`)
  await page.getByRole('button', { name: 'Fork this build' }).click()
  const d = dialog(page)
  await expect(d).toBeVisible()
  await expect(d.getByLabel('Name')).toHaveValue('UX stub fork')
  await expect(d.getByLabel('Name')).toBeFocused()
  await expect(d.getByRole('radio', { name: 'Current files' })).toBeChecked()
  await expect(d.locator('[data-snapshot]')).toHaveCount(2)
  await expect(d.getByRole('list', { name: 'What a fork copies' })).toContainText('Not copied')
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/dialog-current.png` })
  await d.getByLabel('Name').fill('My copy')
  await d.getByRole('button', { name: 'Fork', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/kaisen/${NEW}$`))
  expect(posts).toEqual([{ url: `${SRC}/fork`, body: { name: 'My copy' } }])
  await expect(page.getByText('Forked from UX stub.').first()).toBeVisible()
})

test('Escape closes the dialog and Cancel does not post', async ({ page }) => {
  const posts: { url: string; body: unknown }[] = []
  await login(page)
  await stub(page, { posts })
  await page.goto(`/kaisen/${SRC}`)
  await page.getByRole('button', { name: 'Fork this build' }).click()
  await expect(dialog(page)).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog(page)).toHaveCount(0)
  await page.getByRole('button', { name: 'Fork this build' }).click()
  await dialog(page).getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog(page)).toHaveCount(0)
  expect(posts).toEqual([])
})

test('Fork from here preselects that restore point and sends snapshot_id', async ({ page }) => {
  const posts: { url: string; body: unknown }[] = []
  await login(page)
  await stub(page, { posts })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/kaisen/${SRC}`)
  await page.locator('details:has(> summary #h-restore) summary').click()
  await expect(page.getByRole('button', { name: 'Fork from before restore' })).toBeVisible()
  await page.getByRole('button', { name: 'Fork from before run #2' }).click()
  const d = dialog(page)
  await expect(d.locator('[data-snapshot="2"] input')).toBeChecked()
  await expect(d.getByRole('radio', { name: 'Current files' })).not.toBeChecked()
  await d.getByRole('button', { name: 'Fork', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/kaisen/${NEW}$`))
  expect(posts).toEqual([{ url: `${SRC}/fork`, body: { name: 'UX stub fork', snapshot_id: 2 } }])
})

test('Current files is off during an active run, with the reason; a restore point still works', async ({ page }) => {
  const posts: { url: string; body: unknown }[] = []
  await login(page)
  await stub(page, { posts, src: build(SRC, 'UX stub', { status: 'running', runs: [run(1), run(2, { status: 'running', summary: null, finished_at: null })] }) })
  await page.goto(`/kaisen/${SRC}`)
  await page.getByRole('button', { name: 'Fork this build' }).click()
  const d = dialog(page)
  await expect(d.getByRole('radio', { name: 'Current files' })).toBeDisabled()
  await expect(d.getByTestId('fork-active')).toContainText('A run is in progress. Fork from a restore point, or wait for it to finish.')
  await expect(d.locator('[data-snapshot="2"] input')).toBeChecked() // lands on the newest restore point
  await d.getByRole('button', { name: 'Fork', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/kaisen/${NEW}$`))
  expect(posts[0].body).toEqual({ name: 'UX stub fork', snapshot_id: 2 })
})

test('Fork is disabled with a visible reason when you cannot run the build', async ({ page }) => {
  await login(page)
  await stub(page, { src: build(SRC, 'UX stub', { can_run: false }) })
  await page.goto(`/kaisen/${SRC}`)
  await expect(page.getByRole('button', { name: 'Fork this build' })).toBeDisabled()
  await expect(page.getByTestId('fork-why')).toContainText('You can not fork this build.')
})

test('server refusals show inside the dialog', async ({ page }) => {
  await login(page)
  await stub(page, { forkRes: { status: 403, json: { detail: 'Build limit reached for this project.' } } })
  await page.goto(`/kaisen/${SRC}`)
  await page.getByRole('button', { name: 'Fork this build' }).click()
  await dialog(page).getByRole('button', { name: 'Fork', exact: true }).click()
  await expect(dialog(page).getByRole('alert')).toContainText('Build limit reached for this project.')
  await expect(page).toHaveURL(new RegExp(`/kaisen/${SRC}$`))
})

test('fork page: notice with secret names, composer usable with no runs, no spill', async ({ page }) => {
  await login(page)
  await stub(page, { fork: build(NEW, 'UX stub fork', { runs: [], status: 'idle', forked_from: { id: SRC, name: 'UX stub', snapshot_id: 2, secret_names: ['STRIPE_KEY', 'SMTP_PASS'] } }) })
  for (const [w, h] of sizes) {
    await page.setViewportSize({ width: w, height: h })
    await page.goto(`/kaisen/${NEW}`)
    const n = page.getByTestId('fork-notice')
    await expect(n).toContainText('Forked from UX stub (restore point of')
    await expect(n).toContainText('Send a change request to continue; nothing has run yet.')
    await expect(n).toContainText('Build secrets are not copied. The source had: STRIPE_KEY, SMTP_PASS. Add them under Secrets if this build needs them.')
    await expect(n.locator('.mono')).toHaveText(['STRIPE_KEY', 'SMTP_PASS'])
    await expect(n.getByRole('link', { name: 'UX stub' })).toHaveAttribute('href', `/kaisen/${SRC}`)
    await expect(page.getByLabel('Tell Kaisen what to change')).toBeEnabled()
    await expect(page.getByRole('button', { name: 'Send to Kaisen' })).toBeVisible()
    await expect(page.getByText('This build is a draft')).toHaveCount(0)
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/fork-page-${w}.png` })
    expect(await spillers(page)).toEqual([])
  }
  if (SHOTS) {
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'))
    await page.setViewportSize({ width: 1280, height: 800 })
    await page.screenshot({ path: `${SHOTS}/fork-page-light.png` })
  }
})

test('fork notice: current files and deleted source wording', async ({ page }) => {
  await login(page)
  await stub(page, { fork: build(NEW, 'UX stub fork', { runs: [], status: 'idle', forked_from: { id: SRC, name: 'UX stub', snapshot_id: null, secret_names: [] } }) })
  await page.goto(`/kaisen/${NEW}`)
  await expect(page.getByTestId('fork-notice')).toContainText('Forked from UX stub (current files). Send a change request to continue; nothing has run yet.')
  await expect(page.getByTestId('fork-notice')).not.toContainText('Build secrets')

  await page.unroute(/\/kaisen\/builds\/\d+(\/|\?|$)/)
  await stub(page, { fork: build(NEW, 'UX stub fork', { runs: [], status: 'idle', forked_from: { id: SRC, name: null, snapshot_id: null, secret_names: [] } }) })
  await page.goto(`/kaisen/${NEW}`)
  await expect(page.getByTestId('fork-notice')).toContainText('Forked from a deleted build.')
  await expect(page.getByTestId('fork-notice').getByRole('link')).toHaveCount(0)
})

test('home: forked build shows "fork of <name>" only when the source is listed; no spill', async ({ page }) => {
  const item = (id: number, name: string, extra: Json = {}) => ({
    id, name, status: 'done', app_id: null, app_hostname: null, app_status: null, file_count: 3, cost_usd: 0.1, last_run_at: null, last_error: null,
    open_questions: 0, created_by: { id: 1, email: 'a@b.c' }, created_at: new Date().toISOString(), forked_from_id: null, ...extra,
  })
  const list = [item(1, 'Source one'), item(2, 'Source one fork', { forked_from_id: 1 }), item(3, 'Orphan fork', { forked_from_id: 999 })]
  await page.route('**/kaisen/builds?*', (r) => (r.request().resourceType() === 'fetch' ? r.fulfill({ json: list }) : r.continue()))
  await page.route('**/kaisen/spend?*', (r) => r.fulfill({ json: { last30_usd: 0, today_usd: 0, cap_day: 5 } }))
  await login(page)
  const pid = ((await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]).find((p) => p.is_personal)!.id
  for (const [w, h] of sizes) {
    await page.setViewportSize({ width: w, height: h })
    await page.goto(`/projects/${pid}/kaisen`)
    await expect(page.locator('a:visible', { hasText: 'Orphan fork' }).first()).toBeVisible()
    const metas = page.locator('[data-testid="fork-of"]:visible')
    await expect(metas).toHaveCount(1)
    await expect(metas).toHaveText('fork of Source one')
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/home-${w}.png` })
    expect(await spillers(page)).toEqual([])
  }
})
