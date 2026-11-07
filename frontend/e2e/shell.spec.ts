import { expect, test, type Page } from '@playwright/test'
import { login, spillers } from './helpers'

// The signed-in shell: the Projects > project > app tree on desktop, the Menu on a phone.
const tree = (page: Page) => page.getByRole('navigation', { name: 'Main' })
const projectsNode = (page: Page) => tree(page).locator('details.disc', { has: page.getByRole('link', { name: 'Projects', exact: true }) }).first()
const projectsSummary = (page: Page) => projectsNode(page).locator('> summary')
// The triangle is drawn on the summary's left padding (style.css .disc), not a separate element.
const clickTriangle = (loc: ReturnType<Page['locator']>) => loc.click({ position: { x: 5, y: 11 } })

test('shell tree: toggle, navigate, persist, reveal, aria-current, keyboard, skip link', async ({ page }) => {
  await login(page)

  await test.step('the triangle collapses and expands without navigating', async () => {
    await page.goto('/account')
    await expect(projectsNode(page)).toHaveJSProperty('open', true) // Projects is open by default
    await clickTriangle(projectsSummary(page))
    await expect(projectsNode(page)).toHaveJSProperty('open', false)
    await expect(page).toHaveURL(/\/account$/)
    await clickTriangle(projectsSummary(page))
    await expect(projectsNode(page)).toHaveJSProperty('open', true)
    await expect(page).toHaveURL(/\/account$/)
  })

  await test.step('clicking a name navigates', async () => {
    await tree(page).getByRole('link', { name: 'Templates' }).click()
    await expect(page).toHaveURL(/\/templates$/)
  })

  await test.step('collapse state survives a reload', async () => {
    await clickTriangle(projectsSummary(page))
    await expect(projectsNode(page)).toHaveJSProperty('open', false)
    // the toggle event (which persists the choice) fires a task after the click
    await expect.poll(() => page.evaluate(() => localStorage.getItem('picopaas.nav'))).toContain('"projects":false')
    await page.reload()
    await expect(page.getByRole('heading', { name: 'Templates', level: 1 })).toBeVisible()
    await expect(projectsNode(page)).toHaveJSProperty('open', false)
  })

  await test.step('navigating to a page reveals its path', async () => {
    await page.goto('/apps')
    await expect(projectsNode(page)).toHaveJSProperty('open', true)
    await expect(tree(page).getByRole('link', { name: /^Personal/ })).toBeVisible()
  })

  await test.step('aria-current marks exactly the current page', async () => {
    // In the lists only (the wordmark is a plain router link to /apps and is legitimately current there too).
    const current = tree(page).locator('ul.tree [aria-current="page"]')
    await expect(current).toHaveCount(1) // every project links to /apps, only the current project claims it
    await expect(current).toHaveText(/^Personal/)
    await page.goto('/templates')
    await expect(current).toHaveText('Templates')
    await page.goto('/admin/health')
    await expect(current).toHaveText('Health')
  })

  await test.step('keyboard: Tab reaches the summary, Enter and Space toggle it', async () => {
    await page.goto('/templates')
    const node = projectsNode(page)
    const summary = projectsSummary(page)
    const start = await node.evaluate((el: HTMLDetailsElement) => el.open)
    await page.getByRole('link', { name: 'Skip to content' }).focus()
    // Tab order: skip link, wordmark, then the Projects summary (its link is a child; the summary itself is focusable).
    for (let i = 0; i < 4 && !(await summary.evaluate((el) => el === document.activeElement)); i++) await page.keyboard.press('Tab')
    await expect(summary).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(node).toHaveJSProperty('open', !start)
    await page.keyboard.press('Space')
    await expect(node).toHaveJSProperty('open', start)
    await expect(page).toHaveURL(/\/templates$/)
  })

  await test.step('the skip link moves focus to main', async () => {
    await page.goto('/templates')
    await page.getByRole('link', { name: 'Skip to content' }).focus()
    await page.keyboard.press('Enter')
    await expect(page.locator('#main')).toBeFocused()
  })
})

test.describe('phone shell at 390px', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('Menu opens the tree, Escape closes and returns focus, navigation closes it', async ({ page }) => {
    await login(page)
    await page.goto('/templates')
    const menu = page.getByRole('button', { name: 'Menu' })
    const nav = page.locator('#site-nav')
    await expect(nav).toBeHidden()

    await menu.click()
    await expect(nav).toBeVisible()
    await expect(menu).toHaveAttribute('aria-expanded', 'true')
    await expect(page.getByRole('button', { name: 'Close' })).toBeFocused() // focus moved into the menu

    await page.keyboard.press('Escape')
    await expect(nav).toBeHidden()
    await expect(menu).toBeFocused()

    await menu.click()
    await nav.getByRole('link', { name: 'Docs', exact: true }).click()
    await expect(page).toHaveURL(/\/guide/)
    await expect(nav).toBeHidden()
  })

  test('no horizontal page spill on the main pages', async ({ page }) => {
    await login(page)
    await page.route('**/admin/health/logs/pods', (r) => r.fulfill({ json: { namespaces: [], pods: [] } }))
    const paths = [
      '/apps', '/projects', '/account', '/guide',
      '/admin?tab=users', '/admin?tab=apps', '/admin?tab=projects', '/admin?tab=suspensions',
      '/admin?tab=servers', '/admin?tab=settings', '/admin?tab=audit', '/admin/health',
    ]
    for (const p of paths) {
      await page.goto(p)
      await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible()
      await page.waitForTimeout(250) // let lazy tables and polls settle
      expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth), `${p} document`).toBeLessThanOrEqual(0)
      expect(await spillers(page), `${p} elements past the viewport`).toEqual([])
    }
  })
})

test('project switching from the tree changes the Apps page project', async ({ page }) => {
  await login(page)
  const name = `E2E Shell ${Date.now()}`
  const made = await page.request.post('/projects', { data: { name } })
  expect(made.status()).toBe(201)
  const { id } = (await made.json()) as { id: number }
  try {
    await page.goto('/apps')
    const meta = page.locator('main p.meta').first()
    await expect(meta).toHaveText('Personal')
    await tree(page).getByRole('link', { name: new RegExp(`^${name}`) }).click()
    await expect(page).toHaveURL(/\/apps$/)
    await expect(meta).toHaveText(name)
    await tree(page).getByRole('link', { name: /^Personal/ }).click()
    await expect(meta).toHaveText('Personal')
  } finally {
    await page.request.delete(`/projects/${id}`)
  }
})

test('a 401 sign-out clears the sidebar cache (no previous projects shown to the next sign-in)', async ({ page }) => {
  await login(page)
  const name = `E2E Cache ${Date.now()}`
  const made = await page.request.post('/projects', { data: { name } })
  const { id } = (await made.json()) as { id: number }
  try {
    await page.goto('/apps')
    const mine = tree(page).getByRole('link', { name: new RegExp(`^${name}`) })
    await expect(mine).toBeVisible()

    // Session expiry: the next API call answers 401, the handler in main.ts only sets auth.user = null (no logout button).
    await page.route('**/apps?*', (r) => (r.request().resourceType() === 'fetch' ? r.fulfill({ status: 401, json: { detail: 'Not authenticated' } }) : r.continue()))
    await mine.click() // selecting a project reloads its apps
    await expect(page).toHaveURL(/\/login/)
    await page.unroute('**/apps?*')

    // Sign in again in the same tab (no reload) and hold /projects: the tree must not show the old list meanwhile.
    let release = () => {}
    const gate = new Promise<void>((res) => (release = res))
    await page.route('**/projects', async (r) => {
      if (r.request().resourceType() !== 'fetch') return r.continue()
      await gate
      return r.continue()
    })
    await page.getByLabel('Email').fill((await import('./creds')).ADMIN.email)
    await page.getByLabel('Password', { exact: true }).fill((await import('./creds')).ADMIN.password)
    await page.getByRole('button', { name: 'Log in' }).click()
    await expect(tree(page).getByText('Loading…').first()).toBeVisible()
    await expect(mine).toHaveCount(0)
    release()
    await expect(mine).toBeVisible()
  } finally {
    await page.request.delete(`/projects/${id}`)
  }
})

test('admin Apps tab: the Limits (resources) dialog opens and renders', async ({ page }) => {
  await login(page)
  const v = { cpu_request_m: 50, cpu_limit_m: 200, mem_request_mi: 128, mem_limit_mi: 384, ephemeral_limit_mi: 250, db_max_connections: 10, db_quota_mb: 50, storage_quota_mb: 100, image_size_mb: 512, egress_mbit: 20 }
  const app = {
    id: 501, name: 'stub-app', hostname: 'stub-app.picopaas.xyz', status: 'running', owner_id: 1, owner_email: 'e2e-admin@example.com', image: 'nginx:alpine',
    created_at: '2026-11-05T09:00:00Z', replicas: 1, want_db: false, want_storage: false, db_engine: null, platform_host: null, volume: null, custom_domain: null,
    resources: { ...v, overridden: false, exceeds_platform_caps: false, defaults: v, admin_values: { cpu_request_m: null, cpu_limit_m: null, mem_request_mi: null, mem_limit_mi: null, ephemeral_limit_mi: null, db_max_connections: null, db_quota_mb: null, storage_quota_mb: null, image_size_mb: null, egress_mbit: null } },
  }
  await page.route('**/admin/apps', (r) => (r.request().resourceType() === 'fetch' ? r.fulfill({ json: [app] }) : r.continue()))
  await page.goto('/admin?tab=apps')
  await page.getByRole('button', { name: 'Edit limits for stub-app' }).click()
  const dlg = page.getByRole('dialog', { name: /Limits: stub-app/ })
  await expect(dlg).toBeVisible()
  await expect(dlg.getByRole('heading', { name: 'Replicas' })).toBeVisible()
  await expect(dlg.getByRole('heading', { name: 'Disk, image and data' })).toBeVisible()
  await expect(dlg.getByRole('button', { name: /Save/ })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dlg).toBeHidden()
})
