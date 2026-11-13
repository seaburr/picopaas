import { expect, test, type Page } from '@playwright/test'
import { login, spillers } from './helpers'

const SHOTS = process.env.KAISEN_SHOTS ?? ''
async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true })
}
async function open(page: Page, key = true) {
  await page.route(/\/projects\/\d+\/kaisen(\?|$)/, async (r) => {
    if (r.request().resourceType() !== 'fetch') return r.continue()
    const res = await r.fetch()
    const j = await res.json()
    return r.fulfill({ response: res, json: { ...j, anthropic_key: { ...(j.anthropic_key ?? {}), set: key } } })
  })
  await login(page)
  const pid = ((await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]).find((p) => p.is_personal)!.id
  await page.goto(`/projects/${pid}/kaisen/new`)
  await expect(page.getByTestId('starters')).toBeVisible()
}

for (const w of [1440, 768, 390]) {
  test(`new build at ${w}px: no overflow, states`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: 900 })
    await open(page)
    await shot(page, `empty-${w}`)
    expect(await spillers(page)).toEqual([])
    await page.locator('#kb-brief').fill('Let users upload photos and send email reminders every night. '.repeat(40))
    await shot(page, `long-${w}`)
    expect(await spillers(page)).toEqual([])
  })
}

test('no key state and empty submit errors', async ({ page }) => {
  await open(page, false)
  await expect(page.getByTestId('improve')).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Start building' })).toBeDisabled()
  await shot(page, 'nokey')
})

test('empty submit shows field errors', async ({ page }) => {
  await open(page)
  await page.getByRole('button', { name: 'Start building' }).click()
  await expect(page.getByText('Give the build a name.')).toBeVisible()
  await page.getByLabel('Name', { exact: true }).fill('x')
  await page.getByRole('button', { name: 'Start building' }).click()
  await expect(page.getByText('Describe what the app should do.')).toBeVisible()
  await shot(page, 'errors')
})

test('starter feedback and undo', async ({ page }) => {
  await open(page)
  await page.locator('#kb-brief').fill('my own text')
  await page.locator('[data-use-starter="Kanban board"]').click()
  await expect(page.getByTestId('starter-note')).toContainText('Kanban board')
  await shot(page, 'starter')
  await page.getByRole('button', { name: 'Undo' }).click()
  await expect(page.locator('#kb-brief')).toHaveValue('my own text')
  await expect(page.getByTestId('starter-note')).toHaveCount(0)
})

test('improve errors: 422 refused, 502, 504', async ({ page }) => {
  await open(page)
  for (const [status, detail] of [[422, 'The brief looks like it contains a password, token or key. Remove it before using Improve brief.'], [502, 'The model call failed: x'], [504, 'The model took too long to answer. Try again.']] as const) {
    await page.route('**/kaisen/improve-brief', (r) => r.fulfill({ status, json: { detail } }))
    await page.locator('#kb-brief').fill('A todo list')
    await page.getByTestId('improve').click()
    await expect(page.getByTestId('improve-error')).toContainText(detail)
    await shot(page, `improve-${status}`)
  }
})
