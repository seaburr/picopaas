import { expect, test, type Page } from '@playwright/test'
import { ADMIN } from './creds'

// Collects uncaught page errors, console errors and same-origin 5xx responses for the whole test.
function watch(page: Page) {
  const problems: string[] = []
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') problems.push(`console: ${m.text()}`)
  })
  page.on('response', (r) => {
    if (r.status() >= 500 && new URL(r.url()).origin === new URL(page.url() || r.url()).origin) {
      problems.push(`${r.status()} ${r.url()}`)
    }
  })
  return problems
}

test('smoke: login, browse main pages, log out', async ({ page }) => {
  const problems = watch(page)

  await test.step('login page renders', async () => {
    await page.goto('/')
    await expect(page).toHaveURL(/\/login/)
    await expect(page.getByText('Log in to PicoPaaS')).toBeVisible()
    await expect(page.getByLabel('Email')).toBeVisible()
  })

  await test.step('wrong password shows an error', async () => {
    await page.getByLabel('Email').fill(ADMIN.email)
    await page.getByLabel('Password', { exact: true }).fill('definitely-the-wrong-one')
    await page.getByRole('button', { name: 'Log in' }).click()
    await expect(page.getByRole('alert')).toContainText(/wrong/i)
    // the expected 401 is logged by the browser as a console error; not a defect
    problems.length = 0
  })

  await test.step('log in lands on the admin home', async () => {
    await page.getByLabel('Password', { exact: true }).fill(ADMIN.password)
    await page.getByRole('button', { name: 'Log in' }).click()
    await expect(page).toHaveURL(/\/admin/)
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  })

  await test.step('admin suspensions tab renders its empty state', async () => {
    await page.goto('/admin?tab=suspensions')
    await expect(page.getByRole('tab', { name: /Suspensions/ })).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByText('Nothing is suspended.')).toBeVisible()
  })

  await test.step('projects page lists Personal and links to apps', async () => {
    await page.goto('/projects')
    await expect(page.getByRole('heading', { name: 'Projects', level: 1 })).toBeVisible()
    await expect(page.getByRole('link', { name: 'New project' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Personal' }).first()).toBeVisible()
    await page.getByRole('button', { name: 'Apps in Personal' }).click()
    await expect(page).toHaveURL(/\/apps$/)
  })

  await test.step('apps list renders (empty state)', async () => {
    await page.goto('/apps')
    await expect(page.getByRole('heading', { name: /apps/i, level: 1 })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Deploy your first app' })).toBeVisible()
  })

  await test.step('new app shows the chooser, then the image form', async () => {
    await page.goto('/apps')
    await page.getByRole('link', { name: 'New app' }).first().click()
    await expect(page).toHaveURL(/\/apps\/new$/)
    await expect(page.getByRole('heading', { name: 'Deploy from a template' })).toBeVisible()
    await page.getByRole('link', { name: /Deploy an image/ }).click()
    await expect(page.getByLabel('Image', { exact: true })).toBeVisible()
    await page.goto('/apps/new?sample=1')
    await expect(page.getByRole('heading', { name: 'New app', level: 1 })).toBeVisible()
  })

  await test.step('new app has collapsed resource sliders at their maximum', async () => {
    await page.getByText('Resources', { exact: false }).first().click()
    const slider = page.getByLabel('Memory slider')
    await expect(slider).toBeVisible()
    const max = await slider.getAttribute('max')
    await expect(page.getByLabel('Memory (MB)')).toHaveValue(max ?? '')
    // Every thumb reaches the end of its track at the maximum, even when the ceiling isn't on the step grid.
    const scratch = page.getByLabel('Scratch disk slider')
    await expect(scratch).toHaveValue((await scratch.getAttribute('max')) ?? '')
    await expect(page.getByLabel('Scratch disk (MB)')).toHaveValue('250')
    // Sliding down one notch and back up returns to the real ceiling, not the track's end.
    await scratch.focus()
    await scratch.press('ArrowLeft')
    await expect(page.getByLabel('Scratch disk (MB)')).toHaveValue('248')
    await scratch.press('ArrowRight')
    await expect(page.getByLabel('Scratch disk (MB)')).toHaveValue('250')
    await page.getByLabel('Memory (MB)').fill('128')
    await page.getByLabel('Memory (MB)').press('Tab')
    await expect(slider).toHaveValue('128')
  })

  await test.step('kaisen page renders', async () => {
    await page.goto('/kaisen')
    await expect(page.getByRole('heading', { name: 'Kaisen', level: 1 })).toBeVisible()
    await expect(page.getByRole('heading', { name: /Build your first app with Kaisen/ })).toBeVisible()
  })

  await test.step('account page renders', async () => {
    await page.goto('/account')
    await expect(page.getByRole('heading', { name: 'Account', level: 1 })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Change password' })).toBeVisible()
  })

  await test.step('admin health page renders', async () => {
    // Listing platform pods needs a cluster, and without one the endpoint answers 502 by design. Stub only this call.
    await page.route('**/admin/health/logs/pods', (r) => r.fulfill({ json: { namespaces: [], pods: [] } }))
    await page.goto('/admin/health')
    await expect(page.getByRole('navigation').getByRole('link', { name: 'Health' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Statistics' })).toBeVisible()
    await expect(page.getByText('Apps ever deployed')).toBeVisible()
  })

  await test.step('log out returns to login', async () => {
    await page.getByRole('button', { name: 'Log out' }).click()
    await expect(page).toHaveURL(/\/login/)
    await expect(page.getByText('Log in to PicoPaaS')).toBeVisible()
  })

  expect(problems, problems.join('\n')).toEqual([])
})
