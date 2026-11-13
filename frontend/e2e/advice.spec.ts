import { expect, test, type Page } from '@playwright/test'
import { login, spillers } from './helpers'

// Health advisor UI (phase 1) against the real dev-mode app; the advice endpoints are stubbed with page.route.
const SHOTS = process.env.SHOTS_DIR ?? '' // screenshots only when asked for
const made: number[] = []

async function makeApp(page: Page) {
  const name = `adv-${Date.now().toString(36)}`
  const r = await page.request.post('/apps', { data: { name, image: 'nginxinc/nginx-unprivileged:1.27', port: 8080 } })
  expect(r.status(), await r.text()).toBeLessThan(300)
  const id = ((await r.json()) as { id: number }).id
  made.push(id)
  return { id, name }
}
test.afterEach(async ({ page }) => {
  for (const id of made.splice(0)) await page.request.delete(`/apps/${id}`)
})

const FINDINGS = [
  { rule: 'mem.oom', severity: 'critical', title: 'Out of memory', evidence: 'OOM-killed 3 times in 24 h. Limit 256 MB.', action: { kind: 'set_resource', label: 'Raise memory to 320 MB', field: 'mem_limit_mi', from: 256, to: 320, unit: 'MB', restarts_app: true } },
  { rule: 'image.mutable_tag', severity: 'warning', title: 'Mutable image tag', evidence: 'nginx:latest. Every push redeploys within 10 min.', action: { kind: 'link', label: 'Edit image', to: '/apps/1/edit' } },
  { rule: 'cpu.pinned', severity: 'info', title: 'CPU pinned at limit', evidence: 'At or above 90% of 200 m in 31 of 180 samples.', action: null },
]
const stub = (page: Page, body: unknown, status = 200) =>
  page.route(/\/apps\/\d+\/advice$/, (r) => r.fulfill({ status, json: body }))

test('card is absent with no findings and on a 500', async ({ page }) => {
  await login(page)
  const a = await makeApp(page)
  await stub(page, { findings: [] })
  await page.goto(`/apps/${a.id}`)
  await expect(page.getByRole('heading', { name: 'Status' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Advice' })).toHaveCount(0)
  await page.unroute(/\/apps\/\d+\/advice$/)
  await stub(page, { detail: 'boom' }, 500)
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Status' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Advice' })).toHaveCount(0)
})

test('three rows in order; Apply confirms from → to and sends the owner resource change; no admin wording', async ({ page }) => {
  await login(page)
  const a = await makeApp(page)
  await stub(page, { findings: FINDINGS })
  const sent: Array<{ method: string; url: string; body: unknown }> = []
  await page.route(new RegExp(`/apps/${a.id}$`), async (r) => {
    if (r.request().method() !== 'PATCH') return r.continue()
    sent.push({ method: 'PATCH', url: new URL(r.request().url()).pathname, body: r.request().postDataJSON() })
    const res = await r.fetch({ method: 'GET' })
    return r.fulfill({ response: res })
  })
  await page.goto(`/apps/${a.id}`)
  const table = page.getByRole('table', { name: 'Advice for this app' })
  await expect(table).toBeVisible()
  const rows = table.locator('tbody tr')
  await expect(rows).toHaveCount(3)
  await expect(rows.nth(0)).toContainText('Critical')
  await expect(rows.nth(1)).toContainText('Warning')
  await expect(rows.nth(2)).toContainText('Info')
  await expect(rows.nth(0)).toHaveAttribute('title', 'mem.oom')
  await expect(rows.nth(1).getByRole('link', { name: 'Edit image' })).toHaveAttribute('href', '/apps/1/edit')
  // No action: no button, no link, and no wording of our own about admins.
  await expect(rows.nth(2).getByRole('button')).toHaveCount(0)
  await expect(rows.nth(2).getByRole('link')).toHaveCount(0)
  expect((await table.innerText()).toLowerCase()).not.toContain('admin')
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/advice-app-dark.png` })

  await rows.nth(0).getByRole('button', { name: 'Raise memory to 320 MB' }).click()
  const dlg = page.getByRole('dialog', { name: 'Change memory limit?' })
  await expect(dlg).toContainText('256 MB → 320 MB')
  await expect(dlg).toContainText('Restarts the app')
  await dlg.getByRole('button', { name: 'Cancel' }).click()
  expect(sent).toEqual([])
  await rows.nth(0).getByRole('button', { name: 'Raise memory to 320 MB' }).click()
  await dlg.getByRole('button', { name: 'Apply' }).click()
  await expect.poll(() => sent.length).toBe(1)
  expect(sent[0]).toEqual({ method: 'PATCH', url: `/apps/${a.id}`, body: { resources: { mem_limit_mi: 320 } } })
})

test('rollback action posts the finding\'s digest to /apps/{id}/rollback; a 429 message is shown', async ({ page }) => {
  await login(page)
  const a = await makeApp(page)
  await stub(page, { findings: [{ rule: 'restart.loop', severity: 'critical', title: 'Restart loop', evidence: 'Restarted 6 times in 1 h after the last deploy.', action: { kind: 'rollback', label: 'Roll back to aaaaaaaaaaaa', digest: 'sha256:old', restarts_app: true } }] })
  let body: unknown = null
  await page.route(new RegExp(`/apps/${a.id}/rollback$`), (r) => {
    body = r.request().postDataJSON()
    return r.fulfill({ status: 429, json: { detail: 'Too many changes this hour. Try again later.' } })
  })
  await page.goto(`/apps/${a.id}`)
  await page.getByRole('button', { name: 'Roll back to aaaaaaaaaaaa' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Apply' }).click()
  await expect(page.getByRole('alert').filter({ hasText: 'Too many changes this hour' })).toBeVisible()
  expect(body).toEqual({ digest: 'sha256:old' })
})

test('admin Advice tab: table, links, empty state', async ({ page }) => {
  await login(page)
  await page.route('**/admin/advice', (r) => r.fulfill({ json: { findings: FINDINGS.map((f, i) => ({ ...f, app_id: 10 + i, app_name: `app-${i}`, project_id: 3, project_name: 'Team', owner_email: `u${i}@example.com` })) } }))
  await page.goto('/admin?tab=advice')
  const table = page.getByRole('table', { name: 'Open advice' })
  await expect(table.locator('tbody tr')).toHaveCount(3)
  await expect(table.getByRole('link', { name: 'app-0' })).toHaveAttribute('href', '/apps/10')
  await expect(table.getByRole('button')).toHaveCount(0)
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/advice-admin-dark.png` })
  await page.unroute('**/admin/advice')
  await page.route('**/admin/advice', (r) => r.fulfill({ json: { findings: [] } }))
  await page.reload()
  await expect(page.getByText('No open advice.')).toBeVisible()
})

test('no sideways spill at 1440/1024/768/390; light screenshot', async ({ page }) => {
  await login(page)
  const a = await makeApp(page)
  await stub(page, { findings: FINDINGS })
  await page.route('**/admin/advice', (r) => r.fulfill({ json: { findings: FINDINGS.map((f, i) => ({ ...f, app_id: 10 + i, app_name: `a-long-application-name-${i}`, project_id: 3, project_name: 'A team project', owner_email: `someone-long${i}@example.com` })) } }))
  for (const w of [1440, 1024, 768, 390]) {
    await page.setViewportSize({ width: w, height: 900 })
    for (const url of [`/apps/${a.id}`, '/admin?tab=advice']) {
      await page.goto(url)
      await expect(page.getByRole('table', { name: /advice/i })).toBeVisible()
      await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light')) // visual check only
      expect(await spillers(page), `${url} at ${w}`).toEqual([])
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/advice-${url.startsWith('/admin') ? 'admin' : 'app'}-${w}-light.png` })
    }
  }
})
