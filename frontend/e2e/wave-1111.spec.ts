import { expect, test, type Page } from '@playwright/test'
import { login } from './helpers'

// Apps table (F1) and rejected-user flow (F3) against the real dev-mode app. The add-on flags and ages are patched
// into the real GET /apps response, because dev mode has no database server or bucket to attach.
const made: number[] = []
const pendingUsers: number[] = []

async function makeApp(page: Page, prefix: string) {
  const name = `${prefix}-${Date.now().toString(36)}`
  const r = await page.request.post('/apps', { data: { name, image: 'nginxinc/nginx-unprivileged:1.27', port: 8080 } })
  expect(r.status(), await r.text()).toBeLessThan(300)
  const id = ((await r.json()) as { id: number }).id
  made.push(id)
  return { id, name }
}

test.afterEach(async ({ page }) => {
  for (const id of made.splice(0)) await page.request.delete(`/apps/${id}`)
  for (const id of pendingUsers.splice(0)) await page.request.delete(`/admin/users/${id}`) // the admin session from the test
})

test('apps table: columns, plain-text name, no Delete, add-on check marks, short age', async ({ page }) => {
  await login(page)
  const plain = await makeApp(page, 'tbl-plain')
  const rich = await makeApp(page, 'tbl-rich')
  const ago = (ms: number) => new Date(Date.now() - ms).toISOString()
  await page.route(/\/apps(\?[^/]*)?$/, async (r) => {
    if (!['fetch', 'xhr'].includes(r.request().resourceType()) || r.request().method() !== 'GET') return r.continue()
    const res = await r.fetch()
    const apps = ((await res.json()) as Array<Record<string, unknown>>).map((a) => {
      if (a.id === plain.id) return { ...a, want_db: false, want_storage: false, volume: null, created_at: ago(3 * 86400_000) }
      if (a.id === rich.id) return { ...a, want_db: true, db_engine: 'mysql', want_storage: true, volume: { used_bytes: 0, limit_bytes: 1 }, created_at: ago(12 * 60_000) }
      return a
    })
    return r.fulfill({ response: res, json: apps })
  })
  await page.goto('/apps')
  const table = page.getByRole('table', { name: 'Apps' })
  await expect(table).toBeVisible()
  const heads = await table.locator('thead th').allTextContents()
  expect(heads.map((h) => h.trim())).toEqual(['Name', 'Status', 'Image', 'PSQL', 'MySQL', 'Disk', 'Blob', 'Host', 'Age', 'Actions'])
  const row = (n: string) => table.locator('tbody tr', { hasText: n }).first()
  // The name is plain text, not a link; Health/Restart/Settings remain; Delete is gone.
  await expect(row(plain.name).getByRole('link', { name: plain.name, exact: true })).toHaveCount(0)
  await expect(row(plain.name).getByRole('link', { name: `View health of ${plain.name}` })).toBeVisible()
  await expect(row(plain.name).getByRole('link', { name: `Settings for ${plain.name}` })).toBeVisible()
  await expect(table.getByRole('button', { name: /^Delete/ })).toHaveCount(0)
  // Cells 4-7 are PSQL, MySQL, Disk, Blob (name, status, image come first): mark = visible check + sr-only "Yes".
  const marks = async (n: string) => row(n).locator('td').evaluateAll((tds) => tds.slice(3, 7).map((td) => (td.textContent ?? '').includes('✓')))
  expect(await marks(plain.name)).toEqual([false, false, false, false])
  expect(await marks(rich.name)).toEqual([false, true, true, true])
  await expect(row(rich.name).locator('td').nth(4).locator('.sr-only')).toHaveText('Yes')
  // Age column: compact units, full timestamp in the title.
  const age = async (n: string) => row(n).locator('td').nth(8)
  await expect(await age(plain.name)).toHaveText('3d')
  await expect(await age(rich.name)).toHaveText('12m')
  await expect(await age(plain.name)).toHaveAttribute('title', /\d/)
})

test('admin rejects a pending user, the row shows Approve, approve unlocks sign-in', async ({ page, browser, playwright }) => {
  const email = `e2e-reject-${Date.now().toString(36)}@example.com`
  const password = 'not-the-password-e2e-2'
  // Own request context so the sign-up never shares cookies with the admin page.
  const anon = await playwright.request.newContext({ baseURL: test.info().project.use.baseURL })
  const reg = await anon.post('/auth/register', { data: { email, password, accept_terms: true } })
  expect(reg.status()).toBeLessThan(300)
  await anon.dispose()
  await login(page)
  const users = (await (await page.request.get('/admin/users')).json()) as Array<{ id: number; email: string }>
  pendingUsers.push(users.find((u) => u.email === email)!.id)

  await page.goto('/admin')
  await page.getByRole('button', { name: `Reject ${email}` }).click()
  const dlg = page.getByRole('dialog')
  await expect(dlg).toContainText(`Reject ${email}`)
  await dlg.getByRole('button', { name: 'Reject request' }).click()
  const row = page.locator('tr', { hasText: email })
  // Rejected users are hidden until "Show rejected users" is ticked.
  await expect(row).toHaveCount(0)
  await page.getByTestId('show-rejected').check()
  await expect(row.getByText('Rejected')).toBeVisible()
  await expect(row.getByRole('button', { name: `Approve ${email}` })).toBeVisible()
  await expect(row.getByRole('button', { name: `Suspend ${email}` })).toHaveCount(0)

  // The rejected person sees the clear message, not "Email or password is wrong".
  const ctx = await browser.newContext()
  const p2 = await ctx.newPage()
  await p2.goto('/login')
  await p2.getByLabel('Email').fill(email)
  await p2.getByLabel('Password', { exact: true }).fill(password)
  await p2.getByRole('button', { name: 'Log in' }).click()
  await expect(p2.getByText("Your access request wasn't approved.")).toBeVisible()
  await expect(p2).toHaveURL(/\/login/)

  await row.getByRole('button', { name: `Approve ${email}` }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Approve', exact: true }).click()
  await expect(row.getByText('Rejected')).toHaveCount(0)
  await expect(row.getByRole('button', { name: `Suspend ${email}` })).toBeVisible()
  await p2.getByRole('button', { name: 'Log in' }).click()
  await expect(p2).not.toHaveURL(/\/login/)
  await ctx.close()
})
