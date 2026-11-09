import { expect, test, type Page } from '@playwright/test'
import { login } from './helpers'

// Going from one app's Health (or Settings) straight to another's must show the other app, not keep the first.
// Real apps on the dev server (reconciler off); only their status is reported as running.
async function makeApp(page: Page, prefix: string) {
  const name = `${prefix}-${Date.now().toString(36)}`
  const r = await page.request.post('/apps', { data: { name, image: 'nginxinc/nginx-unprivileged:1.27', port: 8080 } })
  expect(r.status(), await r.text()).toBeLessThan(300)
  return { id: ((await r.json()) as { id: number }).id, name }
}
const h1 = (page: Page) => page.getByRole('heading', { level: 1 })

test("Health to another app's Health, and Settings to Settings, load the other app", async ({ page }) => {
  await login(page)
  const a = await makeApp(page, 'nav-a')
  const b = await makeApp(page, 'nav-b')
  // Report both as running: a queued app's page polls, which would reload the new id by accident and hide the bug.
  await page.route(new RegExp(`/apps/(${a.id}|${b.id})$`), async (r) => {
    if (!['fetch', 'xhr'].includes(r.request().resourceType())) return r.continue()
    const res = await r.fetch()
    return r.fulfill({ response: res, json: { ...(await res.json()), status: 'running' } })
  })

  await page.goto(`/apps/${a.id}`)
  await expect(h1(page)).toContainText(a.name)
  await page.locator('.side .tree a:visible', { hasText: b.name }).first().click()
  await expect(page).toHaveURL(new RegExp(`/apps/${b.id}$`))
  await expect(h1(page)).toContainText(b.name)
  await page.goBack()
  await expect(h1(page)).toContainText(a.name)

  await page.goto(`/apps/${b.id}`) // then client-side to b's Settings
  await page.locator('.side .tree a:visible', { hasText: 'Settings' }).first().click()
  await expect(page).toHaveURL(new RegExp(`/apps/${b.id}/edit$`))
  await expect(page.locator('main')).toContainText(b.name)
  await expect(page.getByText('Something went wrong')).toHaveCount(0)
})
