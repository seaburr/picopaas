import { expect, test, type Page } from '@playwright/test'
import { login, spillers } from './helpers'

// Kaisen builds list: states (loading rows, empty, no key, error, many/long rows) and no horizontal spill at phone width.
const SHOTS = process.env.SHOTS_DIR
const build = (id: number, name: string, status: string, extra: Record<string, unknown> = {}) => ({
  id, name, status, app_id: status === 'done' ? 500 + id : null, app_hostname: status === 'done' ? `b${id}.picopaas.xyz` : null,
  app_status: null, file_count: 7, cost_usd: 0.123, last_run_at: new Date(Date.now() - 3600_000).toISOString(), last_error: null,
  open_questions: 0, created_by: { id: 1, email: 'a@b.c' }, created_at: new Date().toISOString(), ...extra,
})
const LONG = 'A very long build name that keeps going and going to check that the table does not spill sideways at any width'
const BUILDS = [
  build(1, 'jamtunez', 'done'), build(2, LONG, 'done'), build(3, 'Waiting one', 'waiting', { open_questions: 1 }),
  build(4, 'Broken', 'error', { last_error: 'Build failed: ' + 'x'.repeat(200) }), build(5, 'Running', 'running'),
]

async function setup(page: Page, builds: unknown[], keySet: boolean, canEdit = true) {
  await page.route('**/kaisen/builds?*', (r) => (r.request().resourceType() === 'fetch' ? r.fulfill({ json: builds }) : r.continue()))
  await page.route('**/kaisen/spend?*', (r) => r.fulfill({ json: { last30_usd: 1.5, today_usd: 0.2, cap_day: 5 } }))
  await page.route(/\/projects\/\d+\/kaisen(\?|$)/, async (r) => {
    if (r.request().resourceType() !== 'fetch') return r.continue()
    const res = await r.fetch()
    const j = await res.json()
    return r.fulfill({ response: res, json: { ...j, can_edit: canEdit, anthropic_key: { ...(j.anthropic_key ?? {}), set: keySet } } })
  })
  await login(page)
  const list = (await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]
  return list.find((p) => p.is_personal)!.id
}

for (const [w, h] of [[1440, 900], [768, 900], [390, 844]]) {
  test(`list with rows at ${w}`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: h })
    const pid = await setup(page, BUILDS, true)
    await page.goto(`/projects/${pid}/kaisen`)
    await expect(page.getByRole('heading', { level: 1 })).toContainText('Kaisen builds')
    await expect(page.locator('a:visible', { hasText: 'jamtunez' }).first()).toBeVisible()
    expect(await spillers(page)).toEqual([])
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/list-${w}.png`, fullPage: true })
  })
  test(`empty states at ${w}`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: h })
    const pid = await setup(page, [], false)
    await page.goto(`/projects/${pid}/kaisen`)
    await expect(page.getByText('Kaisen needs an Anthropic API key')).toBeVisible()
    expect(await spillers(page)).toEqual([])
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/empty-nokey-${w}.png`, fullPage: true })
  })
}

test('no key with existing builds shows why New build is disabled', async ({ page }) => {
  const pid = await setup(page, BUILDS, false)
  await page.goto(`/projects/${pid}/kaisen`)
  await expect(page.getByRole('button', { name: /^New build/ })).toBeDisabled()
  await expect(page.getByText('Add an Anthropic key in Kaisen settings first')).toBeVisible()
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/nokey-rows.png` })
})

test('non-owner, empty, key set; and error state', async ({ page }) => {
  const pid = await setup(page, [], true, false)
  await page.goto(`/projects/${pid}/kaisen`)
  await expect(page.getByRole('heading', { name: /Build your first app/ })).toBeVisible()
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/empty-ready.png`, fullPage: true })
  await page.unroute('**/kaisen/builds?*')
  await page.route('**/kaisen/builds?*', (r) => (r.request().resourceType() === 'fetch' ? r.fulfill({ status: 500, json: { detail: 'boom' } }) : r.continue()))
  await page.reload()
  await expect(page.getByRole('alert')).toBeVisible()
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/error.png` })
})

test('light theme list', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' })
  const pid = await setup(page, BUILDS, true)
  await page.goto(`/projects/${pid}/kaisen`)
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/list-light.png`, fullPage: true })
})
