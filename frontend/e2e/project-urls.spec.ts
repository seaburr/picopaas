import { expect, test, type Page, type Route } from '@playwright/test'
import { login } from './helpers'

// The project is in the URL for Apps and Kaisen: /projects/:pid/apps[/new], /projects/:pid/kaisen[/new|/settings].
// Old unscoped URLs redirect; the tree puts Kaisen inside each project, after its Apps.
const tree = (page: Page) => page.getByRole('navigation', { name: 'Main' })

async function projects(page: Page, extra?: string) {
  const list = (await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]
  const personal = list.find((p) => p.is_personal)!.id
  let made: { id: number; name: string } | null = null
  if (extra) {
    const r = await page.request.post('/projects', { data: { name: extra } })
    expect(r.status()).toBe(201)
    made = (await r.json()) as { id: number; name: string }
  }
  return { personal, made }
}
const remember = (page: Page, id: number) => page.evaluate((v) => localStorage.setItem('picopaas.project', String(v)), id)
// Patch a JSON API answer without touching page navigations to the same path.
async function patchJson(page: Page, glob: string, patch: (j: Record<string, unknown>) => void) {
  await page.route(glob, async (route: Route) => {
    if (route.request().resourceType() !== 'fetch') return route.continue()
    const res = await route.fetch()
    const j = (await res.json()) as Record<string, unknown>
    patch(j)
    return route.fulfill({ response: res, json: j })
  })
}

test('old URLs redirect to the current project, query kept; unknown project is not found', async ({ page }) => {
  await login(page)
  const { personal, made } = await projects(page, `E2E Redirect ${Date.now()}`)
  try {
    await page.goto('/apps')
    await expect(page).toHaveURL(new RegExp(`/projects/${personal}/apps$`))
    await page.goto('/apps/new?sample=1')
    await expect(page).toHaveURL(new RegExp(`/projects/${personal}/apps/new\\?sample=1$`))
    await page.goto('/kaisen')
    await expect(page).toHaveURL(new RegExp(`/projects/${personal}/kaisen$`))
    await page.goto('/kaisen/new')
    await expect(page).toHaveURL(new RegExp(`/projects/${personal}/kaisen/new$`))
    await page.goto('/kaisen/settings')
    await expect(page).toHaveURL(new RegExp(`/projects/${personal}/kaisen/settings$`))

    // The stored "last clicked" project only decides where the old URLs go.
    await remember(page, made!.id)
    await page.goto('/apps')
    await expect(page).toHaveURL(new RegExp(`/projects/${made!.id}/apps$`))
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(`Apps in ${made!.name}`)

    await page.goto('/projects/987654/apps')
    await expect(page.getByRole('heading', { name: 'Page not found' })).toBeVisible()
    await page.goto('/projects/987654/kaisen/settings')
    await expect(page.getByRole('heading', { name: 'Page not found' })).toBeVisible()
  } finally {
    await page.request.delete(`/projects/${made!.id}`)
  }
})

test("opening project B's Kaisen settings while A was last clicked shows B", async ({ page }) => {
  await login(page)
  const { personal, made } = await projects(page, `E2E Settings ${Date.now()}`)
  try {
    await page.goto(`/projects/${personal}/apps`)
    await remember(page, personal)
    const asked: string[] = []
    page.on('request', (r) => { if (r.resourceType() === 'fetch') asked.push(new URL(r.url()).pathname) })
    await page.goto(`/projects/${made!.id}/kaisen/settings`)
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(`Kaisen settings in ${made!.name}`)
    await expect(page.getByRole('navigation', { name: 'Breadcrumb' })).toContainText(made!.name)
    await expect(page.getByText(`Applies to every build in ${made!.name}. You are an owner.`)).toBeVisible()
    expect(asked).toContain(`/projects/${made!.id}/kaisen`)
    expect(asked).not.toContain(`/projects/${personal}/kaisen`)
    // the tree marks B, not A
    await expect(tree(page).getByRole('link', { name: new RegExp(`^${made!.name}`) })).toHaveClass(/cur/)
    await expect(tree(page).getByRole('link', { name: /^Personal/ })).not.toHaveClass(/cur/)
  } finally {
    await page.request.delete(`/projects/${made!.id}`)
  }
})

test('the tree puts Kaisen inside a project, after its Apps, and nowhere at the top level', async ({ page }) => {
  await login(page)
  const { personal } = await projects(page)
  await page.goto(`/projects/${personal}/apps`)
  await expect(tree(page).locator('ul.tree > li > a', { hasText: 'Kaisen' })).toHaveCount(0)
  await expect(tree(page).locator('ul.tree > li > details > summary a', { hasText: 'Kaisen' })).toHaveCount(0)
  const proj = tree(page).locator('details.disc', { has: page.getByRole('link', { name: /^Personal/ }) }).last()
  const rows = proj.locator(':scope > ul > li')
  await expect(rows).toHaveCount(2)
  await expect(rows.nth(0).locator('> details > summary a')).toHaveText(/^Apps/)
  await expect(rows.nth(1).locator('> details > summary a')).toHaveText(/^Kaisen\s*Preview/)
  // it opens on a Kaisen page, with Builds and Settings
  await page.goto(`/projects/${personal}/kaisen/settings`)
  await expect(rows.nth(1).getByRole('link', { name: 'Builds' })).toBeVisible()
  await expect(rows.nth(1).getByRole('link', { name: 'Settings' })).toHaveAttribute('aria-current', 'page')
})

test('without the Kaisen flag there is no Kaisen row and its URLs go to Apps', async ({ page }) => {
  await login(page)
  const { personal } = await projects(page)
  await patchJson(page, '**/auth/me', (j) => { j.features = { ...(j.features as object), kaisen: false } })
  await page.goto(`/projects/${personal}/kaisen/settings`)
  await expect(page).toHaveURL(new RegExp(`/projects/${personal}/apps$`))
  await expect(tree(page).getByRole('link', { name: /Kaisen/ })).toHaveCount(0)
  await page.goto('/kaisen')
  await expect(page).toHaveURL(new RegExp(`/projects/${personal}/apps$`))
})

test('a member sees the read-only settings and the not-set-up state', async ({ page }) => {
  await login(page)
  const { personal } = await projects(page)
  await patchJson(page, '**/projects/*/kaisen', (j) => { j.can_edit = false; j.anthropic_key = { set: false } })
  await page.goto(`/projects/${personal}/kaisen/settings`)
  await expect(page.getByText('You are a member: you can see what is set, not change it.')).toBeVisible()
  await expect(page.getByText('Only project owners can change these settings.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save settings' })).toHaveCount(0)
  await page.goto(`/projects/${personal}/kaisen`)
  await expect(page.getByText(/Kaisen isn't set up in Personal yet\. Ask an owner to add an Anthropic API key\./)).toBeVisible()
  await expect(page.getByRole('button', { name: /New build/ })).toBeDisabled()
  await expect(page.getByRole('link', { name: 'Open Kaisen settings' })).toHaveCount(0)
})

test('an owner of a project with no key is sent to its settings', async ({ page }) => {
  await login(page)
  const { personal } = await projects(page)
  await patchJson(page, '**/projects/*/kaisen', (j) => { j.can_edit = true; j.anthropic_key = { set: false } })
  await page.goto(`/projects/${personal}/kaisen`)
  await expect(page.getByText('Kaisen needs an Anthropic API key in Personal before it can build.')).toBeVisible()
  await page.getByRole('link', { name: 'Open Kaisen settings' }).click()
  await expect(page).toHaveURL(new RegExp(`/projects/${personal}/kaisen/settings$`))
})

test('switching projects in the tree changes the URL, for Apps and for Kaisen', async ({ page }) => {
  await login(page)
  const { personal, made } = await projects(page, `E2E Switch ${Date.now()}`)
  try {
    await page.goto(`/projects/${personal}/apps`)
    await tree(page).getByRole('link', { name: new RegExp(`^${made!.name}`) }).click()
    await expect(page).toHaveURL(new RegExp(`/projects/${made!.id}/apps$`))
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(`Apps in ${made!.name}`)
    const proj = tree(page).locator('details.disc', { has: page.getByRole('link', { name: new RegExp(`^${made!.name}`) }) }).last()
    await proj.getByRole('link', { name: /^Kaisen/ }).click()
    await expect(page).toHaveURL(new RegExp(`/projects/${made!.id}/kaisen$`))
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(`Kaisen builds in ${made!.name}`)
    await expect(page.getByRole('navigation', { name: 'Breadcrumb' })).toContainText(made!.name)
    // "Other projects" is a row of links to the same page of the others
    await page.locator('#main').getByRole('link', { name: 'Personal', exact: true }).click()
    await expect(page).toHaveURL(new RegExp(`/projects/${personal}/kaisen$`))
  } finally {
    await page.request.delete(`/projects/${made!.id}`)
  }
})

// K128: a slow answer for project A must not overwrite B's list, and A's apps are never shown on B's page.
test('switching projects shows the new project\'s list and ignores a late answer for the old one', async ({ page }) => {
  await login(page)
  const { personal, made } = await projects(page, `E2E Race ${Date.now()}`)
  const fake = {
    id: 999001, owner_id: 1, project_id: personal, name: 'stale-from-a', hostname: 'stale-from-a.example.com', platform_subdomain: null, platform_host: null,
    image: 'nginx:1', port: 80, want_db: false, want_storage: false, status: 'running', status_detail: null, created_at: new Date().toISOString(),
    health_check_enabled: false, health_check_path: '/', env: [],
  }
  try {
    await page.route(`**/apps?project_id=${personal}`, async (route) => {
      if (route.request().resourceType() !== 'fetch') return route.continue()
      await new Promise((r) => setTimeout(r, 2000))
      return route.fulfill({ json: [fake] })
    })
    await page.goto(`/projects/${personal}/apps`)
    await page.goto(`/projects/${made!.id}/apps`, { waitUntil: 'commit' }) // A's request is still pending
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(`Apps in ${made!.name}`)
    await expect(page.getByText('stale-from-a')).toHaveCount(0)
    await page.waitForTimeout(2600) // A's answer has now arrived
    await expect(page.getByText('stale-from-a')).toHaveCount(0)
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(`Apps in ${made!.name}`)
  } finally {
    await page.request.delete(`/projects/${made!.id}`)
  }
})
