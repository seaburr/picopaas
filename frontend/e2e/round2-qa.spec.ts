import { expect, test, type Page } from '@playwright/test'
import { login } from './helpers'

// Feedback round 2, QA coverage: usage gauge thresholds, template POST body, health summary, dark tokens.
// Metrics and health answers are mocked; everything else is the real app.

async function makeApp(page: Page) {
  const name = `r2q-${Date.now().toString(36)}`
  const r = await page.request.post('/apps', { data: { name, image: 'nginxinc/nginx-unprivileged:1.27', port: 8080 } })
  expect(r.status(), await r.text()).toBeLessThan(300)
  return ((await r.json()) as { id: number }).id
}

const metrics = (cpu: number, mem: number) => ({
  points: [
    { ts: new Date(Date.now() - 60_000).toISOString(), cpu_m: cpu, mem_mi: mem },
    { ts: new Date().toISOString(), cpu_m: cpu, mem_mi: mem },
  ],
  cpu_limit_m: 200,
  mem_limit_mi: 400,
  replicas: 1,
  pods: 1,
})

async function gauge(page: Page, label: string) {
  const bar = page.getByRole('progressbar', { name: `${label} usage` })
  const row = bar.locator('xpath=ancestor::li[1]')
  return { bar, row, fill: bar.locator('div').first() }
}

test('usage gauge: ok below 75%, warn from 75%, err from 90% or over cap, with the value text', async ({ page }) => {
  await login(page)
  const id = await makeApp(page)
  try {
    // CPU 60% (ok), memory 75% (warn, boundary)
    let m = metrics(120, 300)
    await page.route(`**/apps/${id}/metrics-history`, (r) => r.fulfill({ json: m }))
    await page.goto(`/apps/${id}`)
    let cpu = await gauge(page, 'CPU')
    let mem = await gauge(page, 'Memory')
    await expect(cpu.row).toContainText('120 / 200 m')
    await expect(cpu.row).toContainText('60%')
    await expect(cpu.bar).toHaveAttribute('aria-valuenow', '60')
    await expect(cpu.fill).toHaveCSS('background-color', await colour(page, '--ok'))
    await expect(cpu.row.locator('.sr-only')).toHaveText('')
    await expect(mem.row).toContainText('300 / 400 MB')
    await expect(mem.row).toContainText('75%')
    await expect(mem.fill).toHaveCSS('background-color', await colour(page, '--warn'))
    await expect(mem.row.locator('.sr-only')).toHaveText('high')

    // 74% is still ok; 90% is err
    m = metrics(148, 360)
    await page.reload()
    cpu = await gauge(page, 'CPU')
    mem = await gauge(page, 'Memory')
    await expect(cpu.row).toContainText('74%')
    await expect(cpu.fill).toHaveCSS('background-color', await colour(page, '--ok'))
    await expect(mem.row).toContainText('90%')
    await expect(mem.fill).toHaveCSS('background-color', await colour(page, '--err'))
    await expect(mem.row.locator('.sr-only')).toHaveText('critical')

    // Database over its cap is err even though the bar is clamped at 100%
    await page.route(new RegExp(`/apps/${id}$`), async (route) => {
      if (route.request().resourceType() !== 'fetch') return route.continue()
      const res = await route.fetch()
      const j = (await res.json()) as Record<string, unknown>
      j.quota = { db: { size_bytes: 60 * 1024 * 1024, limit_bytes: 50 * 1024 * 1024, over_quota: true } }
      return route.fulfill({ response: res, json: j })
    })
    await page.reload()
    const db = await gauge(page, 'Database')
    await expect(db.row).toContainText('60 / 50 MB')
    await expect(db.bar).toHaveAttribute('aria-valuenow', '100')
    await expect(db.fill).toHaveCSS('background-color', await colour(page, '--err'))
    await expect(db.row.locator('.sr-only')).toHaveText('over cap')
  } finally {
    await page.request.delete(`/apps/${id}`)
  }
})

// The computed colour of a CSS variable, as the browser reports background-color.
async function colour(page: Page, v: string) {
  return page.evaluate((name) => {
    const el = document.createElement('i')
    el.style.background = `var(${name})`
    document.body.appendChild(el)
    const c = getComputedStyle(el).backgroundColor
    el.remove()
    return c
  }, v)
}

test('template flow: the app is created in the project chosen in the project step', async ({ page }) => {
  await login(page)
  const list = (await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]
  const personal = list.find((p) => p.is_personal)!.id
  const r = await page.request.post('/projects', { data: { name: `E2E TplPost ${Date.now()}` } })
  expect(r.status()).toBe(201)
  const other = (await r.json()) as { id: number; name: string }
  try {
    const posted: Record<string, unknown>[] = []
    await page.route('**/apps', (route) => {
      if (route.request().method() !== 'POST') return route.fallback()
      posted.push(route.request().postDataJSON() as Record<string, unknown>)
      return route.fulfill({ status: 400, json: { detail: 'stopped by test' } }) // nothing is created
    })
    await page.goto(`/templates?project=${personal}`)
    await page.getByRole('button', { name: 'Use this template' }).first().click()
    await page.getByLabel('Project').selectOption(String(other.id))
    await page.getByRole('button', { name: 'Continue' }).click()
    await expect(page).toHaveURL(new RegExp(`/projects/${other.id}/apps/new\\?template=`))
    await expect(page.getByRole('heading', { level: 1 })).toContainText(`New app in ${other.name}`)
    await page.getByLabel('Name', { exact: true }).fill(`r2tpl-${Date.now().toString(36)}`)
    await page.locator('form button[type=submit]').last().click()
    await expect.poll(() => posted.length).toBe(1)
    expect(posted[0].project_id).toBe(other.id)
  } finally {
    await page.request.delete(`/projects/${other.id}`)
  }
})

test('template flow: with one project the project step is skipped', async ({ page }) => {
  await login(page)
  const list = (await (await page.request.get('/projects')).json()) as { id: number }[]
  test.skip(list.length !== 1, 'other specs left extra projects')
  await page.goto(`/templates?project=${list[0].id}`)
  await page.getByRole('button', { name: 'Use this template' }).first().click()
  await expect(page).toHaveURL(new RegExp(`/projects/${list[0].id}/apps/new\\?template=`))
  await expect(page.getByLabel('Project')).toHaveCount(0)
})

test('health page: one problems box, the problem is listed once, no Admin page link', async ({ page }) => {
  await login(page)
  const now = new Date().toISOString()
  await page.route('**/admin/health', (route) => {
    if (route.request().resourceType() !== 'fetch') return route.continue()
    return route.fulfill({
      json: {
        generated_at: now,
        metrics_available: false,
        errors: ['metrics.k8s.io unavailable'],
        cluster: { node_count: 2, max_nodes: 3, pods_total: 10 },
        nodes: [],
        apps: [],
        platform: [],
        db_servers: [],
        users: { total: 3, active: 3, pending: 0, suspended: 0, admins: 1 },
      },
    })
  })
  await page.goto('/admin/health')
  await expect(page.getByRole('heading', { name: 'Cluster health', level: 1 })).toBeVisible()
  const box = page.getByRole('status', { name: 'Overall status' })
  await expect(box).toHaveCount(1)
  await expect(box).toContainText('1 problem')
  await expect(box.getByText('Metrics unavailable')).toHaveCount(1)
  await expect(page.locator('#main .alert-warn')).toHaveCount(1)
  await expect(page.locator('#main').getByRole('link', { name: /Admin page/ })).toHaveCount(0)
  await expect(page.locator('#main').getByText('Admin page')).toHaveCount(0)
})

test('dark theme tokens: softer ink', async ({ page }) => {
  await page.goto('/login')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  const v = await page.evaluate(() => {
    const cs = getComputedStyle(document.documentElement)
    return { fg: cs.getPropertyValue('--fg').trim(), mut: cs.getPropertyValue('--mut').trim() }
  })
  expect(v).toEqual({ fg: '#d4d4d8', mut: '#a0a5ac' })
})
