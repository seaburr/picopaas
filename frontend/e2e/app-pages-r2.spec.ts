import { expect, test } from '@playwright/test'
import { login } from './helpers'

// Feedback round 2, batch A: restart asks first (apps list), the edit page has no cramped side column.
async function makeApp(page: import('@playwright/test').Page) {
  const name = `r2a-${Date.now().toString(36)}`
  const r = await page.request.post('/apps', { data: { name, image: 'nginxinc/nginx-unprivileged:1.27', port: 8080, want_db: true, want_storage: true } })
  expect(r.status(), await r.text()).toBeLessThan(300)
  return { id: ((await r.json()) as { id: number }).id, name }
}

test('restart from the apps list asks before it restarts', async ({ page }) => {
  await login(page)
  const { id, name } = await makeApp(page)
  let restarts = 0
  await page.route(`**/apps/${id}/restart`, (route) => {
    restarts++
    return route.fulfill({ status: 202, json: {} })
  })
  const projects = (await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]
  await page.goto(`/projects/${projects.find((p) => p.is_personal)!.id}/apps`)
  const restart = page.getByRole('button', { name: `Restart ${name}` }).first()
  await restart.click()
  const dlg = page.getByRole('dialog', { name: 'Restart app?' })
  await expect(dlg).toBeVisible()
  await dlg.getByRole('button', { name: 'Cancel' }).click()
  await expect(dlg).toBeHidden()
  expect(restarts).toBe(0)
  await restart.click()
  await dlg.getByRole('button', { name: 'Restart', exact: true }).click()
  await expect.poll(() => restarts).toBe(1)
  await page.request.delete(`/apps/${id}`) // leave the shared admin project empty for smoke.spec's empty state
})

test('edit page: other settings and the danger zone sit full width under the form', async ({ page }) => {
  await login(page)
  const { id } = await makeApp(page)
  try {
  for (const size of [{ width: 1440, height: 900 }, { width: 390, height: 800 }]) {
    await page.setViewportSize(size)
    await page.goto(`/apps/${id}/edit`)
    const form = page.locator('form.sec').first()
    const other = page.getByTestId('other-settings')
    await expect(other).toBeVisible()
    const fb = (await form.boundingBox())!
    const ob = (await other.boundingBox())!
    expect(ob.y).toBeGreaterThan(fb.y + fb.height - 1) // below, not beside
    expect(Math.abs(ob.width - fb.width)).toBeLessThan(2) // same width
    const danger = page.locator('.sec-danger')
    const db = (await danger.boundingBox())!
    expect(db.width).toBeGreaterThan(Math.min(size.width - 80, 600))
    const p = danger.locator('p').first()
    expect((await p.boundingBox())!.height).toBeLessThan(60) // a sentence, not a column of single words
  }
  } finally {
    await page.request.delete(`/apps/${id}`)
  }
})
