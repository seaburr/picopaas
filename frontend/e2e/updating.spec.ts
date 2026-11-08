import { expect, test, type Page } from '@playwright/test'
import { login } from './helpers'

const SHOTS = process.env.SHOTS_DIR
const shot = async (page: Page, name: string) => {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true })
}

// A running app whose new version is rolling out reads as running, never Queued or Deploying.
test('updating app shows Running · updating on the list and the app page', async ({ page }) => {
  await login(page)
  const r = await page.request.post('/apps', { data: { name: `upd-${Date.now().toString(36)}`, image: 'nginxinc/nginx-unprivileged:1.27', port: 8080 } })
  expect(r.ok()).toBeTruthy()
  const id = ((await r.json()) as { id: number }).id
  await page.route(/\/apps(\/\d+)?(\?.*)?$/, async (route) => {
    if (route.request().resourceType() === 'document') return route.continue()
    const res = await route.fetch()
    const body = await res.json()
    const patch = (a: { id: number }) => (a.id === id ? { ...a, status: 'deploying', updating: true } : a)
    await route.fulfill({ response: res, json: Array.isArray(body) ? body.map(patch) : patch(body) })
  })
  await page.goto('/apps')
  const row = page.getByRole('row').filter({ hasText: 'upd-' }).first()
  await expect(row).toContainText('Running · updating')
  await expect(row).not.toContainText(/Queued|Deploying|Starting/)
  await shot(page, 'updating-list')
  await page.goto(`/apps/${id}`)
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  await expect(page.getByText('Running · updating').first()).toBeVisible()
  await expect(page.getByText("A new version is being rolled out. The current version stays live until it's ready.")).toBeVisible()
  await expect(page.getByText(/Queued|Deploying|Starting your app/)).toHaveCount(0)
  await shot(page, 'updating-app')
})
