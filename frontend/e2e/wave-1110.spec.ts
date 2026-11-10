import { expect, test, type Page } from '@playwright/test'
import { login } from './helpers'

const SHOTS = process.env.SHOTS_DIR
const shot = async (page: Page, name: string) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` }) }

test('pages for a missing id show a proper not-found state with no stray hairline', async ({ page }) => {
  await login(page)
  for (const [path, title] of [['/apps/999999', 'App not found'], ['/apps/999999/edit', 'App not found'], ['/projects/999999', 'Project not found'], ['/kaisen/999999', 'Build not found']] as const) {
    await page.goto(path)
    const nf = page.getByTestId('not-found')
    await expect(nf.getByRole('heading', { name: title })).toBeVisible()
    await expect(nf).toContainText("It doesn't exist, or your account can't see it.")
    await expect(nf.getByRole('link', { name: 'Apps' })).toBeVisible()
    await expect(nf.getByRole('link', { name: 'Projects' })).toBeVisible()
    // No section top rule inside <main> above the heading.
    const rules = await page.locator('main .sec').count()
    expect(rules, `${path}: stray .sec`).toBe(0)
    if (path === '/apps/999999') await shot(page, 'not-found-app')
  }
})

test('banner: rendered from /api/banner, plain text, nothing when disabled', async ({ page }) => {
  await page.route('**/api/banner', (r) => r.fulfill({ json: { enabled: true, level: 'info', title: 'Heads up', text: '<b>x</b> maintenance' } }))
  await page.goto('/login')
  const b = page.getByRole('note')
  await expect(b).toContainText('Heads up <b>x</b> maintenance')
  await expect(b).toHaveClass(/alert-info/)
  await expect(b.locator('b')).toHaveCount(0)
  await shot(page, 'banner-info')
})

test('banner: hidden when disabled or on error', async ({ page }) => {
  await page.route('**/api/banner', (r) => r.fulfill({ status: 500, json: { detail: 'boom' } }))
  await page.goto('/login')
  await expect(page.getByRole('button', { name: 'Log in' })).toBeVisible()
  await expect(page.getByRole('note')).toHaveCount(0)
})

test('admin edits the banner with a live preview and saves; PUT detail is shown on 422', async ({ page }) => {
  let saved = { enabled: false, level: 'warning', title: '', text: '' }
  await page.route('**/admin/banner', async (r) => {
    if (r.request().method() === 'PUT') {
      const body = r.request().postDataJSON()
      if (body.title === 'bad') return r.fulfill({ status: 422, json: { detail: 'title: not allowed' } })
      saved = body
    }
    return r.fulfill({ json: saved })
  })
  await login(page)
  await page.goto('/admin')
  await page.getByRole('tab', { name: /Settings/ }).click()
  const sec = page.getByTestId('admin-banner')
  await expect(sec.getByRole('heading', { name: 'Banner' })).toBeVisible()
  await sec.getByLabel('Title').fill('bad')
  await sec.getByRole('button', { name: 'Save banner' }).click()
  await expect(sec.getByRole('alert')).toContainText('title: not allowed')
  await sec.getByLabel('Title').fill('Planned work')
  await sec.getByLabel('Text').fill('Back at noon')
  await sec.getByLabel('Show the banner').check()
  await sec.getByLabel('Info').check()
  await expect(sec.getByTestId('banner-preview')).toContainText('Planned work Back at noon')
  await expect(sec).toContainText('12 / 80')
  await shot(page, 'admin-banner')
  await sec.getByRole('button', { name: 'Save banner' }).click()
  await expect(page.getByRole('note').first()).toContainText('Planned work')
  expect(saved.level).toBe('info')
})

test('new app: PostgreSQL option explains why it is disabled', async ({ page }) => {
  await login(page)
  const me = await (await page.request.get('/auth/me')).json()
  await page.route('**/auth/me', (r) => r.fulfill({ json: { ...me, features: { ...me.features, databases: true, db_engines: ['mysql'] } } }))
  const list = (await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]
  await page.goto(`/projects/${list.find((p) => p.is_personal)!.id}/apps/new?from=image`)
  await page.getByRole('checkbox', { name: /^Database/ }).check()
  await expect(page.getByTestId('pg-unavailable')).toHaveText('PostgreSQL is not available. Contact your platform administrator.')
  await expect(page.getByRole('radio', { name: 'PostgreSQL' })).toBeDisabled()
})
