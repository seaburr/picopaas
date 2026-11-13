import { expect, test, type Page } from '@playwright/test'
import { login } from './helpers'

// B5 preview pane on the build page, with a stubbed build and a stubbed app host (no cluster).
const ID = 98
const uploadsOk = { uploads: [], quota: {}, notice: '', uploads_enabled: true, uploads_project_enabled: true, uploads_platform_enabled: true, uploads_disabled_reason: null, uploads_not_enabled_label: '', uploads_notice: '' }
const summaryOk = { count: 0, asset_count: 0, reference_count: 0, asset_bytes: 0, ref_bytes: 0, max_files: 20, asset_limit_bytes: 1e6, ref_limit_bytes: 1e6, changed_since_deploy: false, ...uploadsOk }
function build(status: string): Record<string, unknown> {
  return {
    id: ID, name: 'Preview stub', status: 'done', app_id: 7, app_hostname: 'stub-app.picopaas.xyz', app_status: status, file_count: 0, cost_usd: 0.1,
    last_run_at: null, last_error: null, open_questions: 0, created_by: { id: 1, email: 'e2e-admin@example.com' }, created_at: '2026-11-05T09:00:00Z',
    brief: 'x', project_id: 1, slug: 'preview-stub', files: [], runs: [], inbox: [],
    spend: { build_usd: 0.1, cap_build: 5, month_usd: 0.1, last30_usd: 0.1 },
    output: { mode: 'download', repo: null, branch: null, push_on_finish: false, open_pr: false, auth: 'project', token: { set: false }, project_token_set: false, can_push: false },
    can_run: true, uploads: summaryOk,
  }
}
async function stub(page: Page, status: string) {
  await page.route(new RegExp(`/kaisen/builds/${ID}(/|\\?|$)`), (route) => {
    const req = route.request()
    if (req.resourceType() !== 'fetch' && req.resourceType() !== 'xhr') return route.continue()
    const path = new URL(req.url()).pathname.replace(`/kaisen/builds/${ID}`, '')
    return route.fulfill({ json: req.method() === 'GET' && path === '' ? build(status) : path === '/uploads' ? uploadsOk : [] })
  })
  await page.route('**/projects/*/kaisen', (r) => r.fulfill({ status: 404, json: { detail: 'none' } }))
  await page.route('**/apps/7', (r) => r.fulfill({ json: { id: 7, name: 'a', status, hostname: 'stub-app.picopaas.xyz', updating: false } }))
  await page.route('https://stub-app.picopaas.xyz/**', (r) => r.fulfill({ contentType: 'text/html', body: '<p>hi</p>' }))
}
const pane = (page: Page) => page.getByText('Preview pane')

test('preview pane: width presets and exact sandbox', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  await login(page)
  await stub(page, 'running')
  await page.goto(`/kaisen/${ID}`)
  const frame = page.getByTestId('preview-frame')
  await expect(frame).toBeVisible()
  await expect(frame).toHaveAttribute('sandbox', 'allow-scripts allow-forms allow-popups')
  await expect(frame).toHaveAttribute('referrerpolicy', 'no-referrer')
  await page.getByRole('button', { name: 'Phone' }).click()
  await expect.poll(() => frame.evaluate((f) => (f as HTMLElement).offsetWidth)).toBe(390)
  expect((await frame.boundingBox())!.width).toBeLessThanOrEqual(390)
  await page.getByRole('button', { name: 'Tablet' }).click()
  await expect.poll(() => frame.evaluate((f) => (f as HTMLElement).offsetWidth)).toBe(768)
  await expect(page.getByTestId('preview-scale')).toContainText('768 × 1024 px, shown at')
  await page.getByRole('button', { name: 'Desktop' }).click()
  await expect.poll(() => frame.evaluate((f) => (f as HTMLElement).offsetWidth)).toBe(1280)
  expect((await frame.boundingBox())!.width).toBeLessThan(400) // scaled to fit the side panel, no sideways scroll
  await expect(page.getByRole('link', { name: /Open in new tab/ }).first()).toHaveAttribute('href', 'https://stub-app.picopaas.xyz/')
})

test('preview pane: fallback when the app is not running; collapsed and no sideways scroll at 390', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 })
  await login(page)
  await stub(page, 'error')
  await page.goto(`/kaisen/${ID}`)
  await expect(page.getByTestId('preview-frame')).toHaveCount(0)
  await page.getByText('Build details').click()
  await expect(pane(page)).toBeVisible()
  await expect(page.getByTestId('preview-fallback')).toHaveCount(0) // collapsed on a phone
  await pane(page).click()
  await expect(page.getByTestId('preview-fallback')).toContainText('not reachable')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})
