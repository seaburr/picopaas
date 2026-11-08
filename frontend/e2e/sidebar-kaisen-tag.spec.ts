import { expect, test, type Page } from '@playwright/test'
import { login } from './helpers'

// Sidebar app rows keep their Kaisen mark: full "Kaisen" tag when the name fits, compact "K" when it does not.
const SHOTS = process.env.SHOTS_DIR
const app = (id: number, name: string, kaisen: boolean) => ({
  id, name, hostname: `a${id}.picopaas.xyz`, status: 'running', status_detail: null, env: [], image_history: [],
  kaisen_build_id: kaisen ? id : null,
})
const APPS = [app(901, 'jamtunez', true), app(902, 'Platformer', true), app(903, 'Guitar Tuner', true), app(904, 'A really long app name here', true), app(905, 'Plain long name app', false)]

async function stubApps(page: Page) {
  await page.route(/\/apps\?project_id=\d+$/, (r) =>
    r.request().resourceType() === 'fetch' || r.request().resourceType() === 'xhr' ? r.fulfill({ json: APPS }) : r.continue())
}
const row = (page: Page, name: string) => page.locator('.side .tree a:visible', { has: page.locator('.nm', { hasText: new RegExp(`^${name}$`) }) }).first()

test('every Kaisen app keeps a mark; short names get the full tag, long ones K', async ({ page }) => {
  await login(page)
  await stubApps(page)
  const list = (await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]
  await page.goto(`/projects/${list.find((p) => p.is_personal)!.id}/apps`)
  await expect(row(page, 'jamtunez')).toBeVisible()
  await expect(row(page, 'jamtunez').locator('.ktag')).toHaveText(/^Kaisen/)
  for (const n of ['A really long app name here']) {
    const tag = row(page, n).locator('.ktag')
    await expect(tag).toBeVisible()
    await expect(tag.locator('[aria-hidden]')).toHaveText('K')
    await expect(tag).toHaveAttribute('title', /Built with Kaisen/)
    await expect(tag).toContainText('Kaisen') // screen-reader text
  }
  for (const n of ['Platformer', 'Guitar Tuner']) await expect(row(page, n).locator('.ktag')).toBeVisible()
  await expect(row(page, 'Plain long name app').locator('.ktag')).toHaveCount(0)
  // The tag never pushes past the row: its right edge stays inside the link.
  for (const n of APPS.filter((a) => a.kaisen_build_id).map((a) => a.name)) {
    const r = row(page, n)
    const [a, t] = await Promise.all([r.boundingBox(), r.locator('.ktag').boundingBox()])
    expect(t!.x + t!.width).toBeLessThanOrEqual(a!.x + a!.width + 0.5)
    // The full tag only ever sits beside a name that is not clipped.
    const full = (await r.locator('.ktag [aria-hidden]').textContent()) === 'Kaisen'
    const clipped = await r.locator('.nm').evaluate((el) => {
      const rg = document.createRange()
      rg.selectNodeContents(el)
      return rg.getBoundingClientRect().width > el.getBoundingClientRect().width + 0.01
    })
    expect(full && clipped, `${n}: full tag beside a clipped name`).toBe(false)
  }
  if (SHOTS) await page.locator('.side').screenshot({ path: `${SHOTS}/sidebar-ktag.png` })
})
