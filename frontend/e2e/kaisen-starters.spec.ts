import { expect, test } from '@playwright/test'
import { checkBrief } from '../src/composables/briefCheck'
import { STARTERS } from '../src/composables/kaisenStarters'
import { login, spillers } from './helpers'

// Module-level rules (pure functions, no browser needed).
const ids = (b: string) => checkBrief(b).map((w) => w.id)

test('brief check rules', () => {
  expect(ids('')).toEqual([])
  expect(ids('Build a todo list that saves to MySQL.')).toEqual([])
  expect(ids('Build a todo list where users can upload photos.')).toContain('uploads-storage')
  expect(ids('Build a notes app with file uploads kept in a storage bucket.')).not.toContain('uploads-storage')
  expect(ids('Build a todo list. It saves the todos.')).toContain('no-storage')
  expect(ids('Build a todo list that saves to localStorage.')).toEqual([])
  expect(ids('Build a todo list stored in a database.')).toContain('db-engine')
  expect(ids('Build a static page with a login and a MySQL database.')).toContain('static-conflict')
  expect(ids('Send an email to every user when a todo is due. Save to MySQL.')).toContain('email')
  expect(ids('Run a nightly cron job that cleans old rows. Save to MySQL.')).toContain('cron')
  // "out of scope" and "no email" are not requests
  expect(ids('Save to MySQL. Out of scope: email notifications, cron jobs.')).toEqual([])
  expect(ids('Save to MySQL with no email and no background jobs.')).toEqual([])
})

test('every starter passes its own brief check', () => {
  for (const s of STARTERS) expect(checkBrief(s.brief), s.title).toEqual([])
  expect(STARTERS.map((s) => s.title)).toEqual(expect.arrayContaining(['Kanban board', 'Notes with uploads', 'Small CRM']))
  for (const s of STARTERS) if (s.cost) expect(s.costSource, s.title).toBeTruthy()
})

test('starter gallery fills the form without starting a build; warnings never block Start', async ({ page }) => {
  let created = 0
  await page.route('**/kaisen/builds', (r) => { if (r.request().method() === 'POST') created++; return r.continue() })
  // Start is disabled without a project key; stub settings so the button state reflects only the brief check.
  await page.route(/\/projects\/\d+\/kaisen(\?|$)/, async (r) => {
    if (r.request().resourceType() !== 'fetch') return r.continue()
    const res = await r.fetch()
    const j = await res.json()
    return r.fulfill({ response: res, json: { ...j, anthropic_key: { ...(j.anthropic_key ?? {}), set: true } } })
  })
  await login(page)
  const list = (await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]
  const pid = list.find((p) => p.is_personal)!.id
  await page.goto(`/projects/${pid}/kaisen/new`)
  const gallery = page.getByTestId('starters')
  await expect(gallery.locator('tbody tr')).toHaveCount(STARTERS.length)
  const row = gallery.locator('[data-starter="Notes with uploads"]')
  await expect(row).toContainText('MySQL')
  await expect(row).toContainText('Bucket')
  await expect(gallery.locator('[data-starter="Fancy Docs"]')).toContainText('~$0.05')
  await expect(gallery.locator('[data-starter="Small CRM"]')).toContainText('~$0.06')
  await expect(gallery.locator('[data-starter="Kanban board"] td[data-label="Cost"]')).toHaveText('')
  await page.locator('[data-use-starter="Notes with uploads"]').click()
  await expect(page.getByLabel('Name', { exact: true })).toHaveValue('Notes with uploads')
  await expect(page.locator('#kb-brief')).toHaveValue(/storage bucket/)
  await expect(page.getByTestId('brief-warnings')).toHaveCount(0)
  expect(created).toBe(0)
  // warnings next to the brief, Start stays enabled
  await page.locator('#kb-brief').fill('Let users upload photos and send email reminders every night.')
  const w = page.getByTestId('brief-warnings')
  await expect(w.locator('[data-warning="uploads-storage"]')).toBeVisible()
  await expect(w.locator('[data-warning="email"]')).toBeVisible()
  await expect(w.locator('[data-warning="cron"]')).toBeVisible()
  const start = page.getByRole('button', { name: 'Start building' })
  await expect(start).toBeEnabled()
  expect(created).toBe(0)
})

test('gallery fits at phone width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 })
  await login(page)
  const list = (await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]
  await page.goto(`/projects/${list.find((p) => p.is_personal)!.id}/kaisen/new`)
  await expect(page.getByTestId('starters')).toBeVisible()
  expect(await spillers(page)).toEqual([])
})

async function openNew(page: import('@playwright/test').Page) {
  await page.route(/\/projects\/\d+\/kaisen(\?|$)/, async (r) => {
    if (r.request().resourceType() !== 'fetch') return r.continue()
    const res = await r.fetch()
    const j = await res.json()
    return r.fulfill({ response: res, json: { ...j, anthropic_key: { ...(j.anthropic_key ?? {}), set: true } } })
  })
}

test('PostgreSQL starter is disabled with the amber text when no PostgreSQL server exists', async ({ page }) => {
  await openNew(page)
  await page.route('**/templates', async (r) => {
    if (r.request().resourceType() !== 'fetch') return r.continue()
    const res = await r.fetch()
    const list = (await res.json()) as Record<string, unknown>[]
    const patched = list.map((t) => (t.wants_db && t.db_engine === 'pg' ? { ...t, available: false } : t))
    return r.fulfill({ response: res, json: patched })
  })
  await login(page)
  const pid = ((await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]).find((p) => p.is_personal)!.id
  await page.goto(`/projects/${pid}/kaisen/new`)
  const crm = page.locator('[data-starter="Small CRM"]')
  await expect(crm.getByTestId('pg-off')).toHaveText('PostgreSQL is not available. Contact your platform administrator.')
  await expect(page.locator('[data-use-starter="Small CRM"]')).toBeDisabled()
  await expect(page.locator('[data-use-starter="Kanban board"]')).toBeEnabled()
  await expect(page.getByTestId('pg-off')).toHaveCount(1)
})

test('Improve brief: suggest, Keep mine, then Use this; nothing is replaced without a click', async ({ page }) => {
  await openNew(page)
  let calls = 0
  let sent = ''
  await page.route('**/kaisen/improve-brief', (r) => {
    calls++
    sent = r.request().postData() ?? ''
    return r.fulfill({ json: { brief: 'Build a todo list. Save to MySQL.\n\nAcceptance checks:\n- Adding a todo shows it', model: 'claude-haiku-5-5', usd: 0.0003 } })
  })
  await login(page)
  const pid = ((await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]).find((p) => p.is_personal)!.id
  await page.goto(`/projects/${pid}/kaisen/new`)
  const improve = page.getByTestId('improve')
  await expect(improve).toBeDisabled() // empty brief
  await page.locator('#kb-brief').fill('A todo list')
  expect(calls).toBe(0) // never automatic
  await improve.click()
  await expect(page.getByTestId('suggestion-text')).toContainText('Acceptance checks')
  expect(calls).toBe(1)
  expect(JSON.parse(sent)).toMatchObject({ project_id: pid, brief: 'A todo list' })
  await expect(page.locator('#kb-brief')).toHaveValue('A todo list')
  await page.getByTestId('suggestion-keep').click()
  await expect(page.getByTestId('suggestion')).toHaveCount(0)
  await expect(page.locator('#kb-brief')).toHaveValue('A todo list')
  await improve.click()
  await page.getByTestId('suggestion-use').click()
  await expect(page.locator('#kb-brief')).toHaveValue(/Save to MySQL/)
  await expect(page.getByTestId('suggestion')).toHaveCount(0)
  expect(calls).toBe(2)
})

test('Improve brief shows the server error and stays usable; phone width has no overflow', async ({ page }) => {
  await openNew(page)
  await page.route('**/kaisen/improve-brief', (r) => r.fulfill({ status: 429, json: { detail: 'Too many attempts. Wait a few minutes and try again.' } }))
  await page.setViewportSize({ width: 390, height: 800 })
  await login(page)
  const pid = ((await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]).find((p) => p.is_personal)!.id
  await page.goto(`/projects/${pid}/kaisen/new`)
  await page.locator('#kb-brief').fill('A todo list')
  await page.getByTestId('improve').click()
  await expect(page.getByTestId('improve-error')).toContainText('Too many attempts')
  await expect(page.getByTestId('improve')).toBeEnabled()
  expect(await spillers(page)).toEqual([])
})
