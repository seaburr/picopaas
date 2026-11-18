import { expect, test, type Page } from '@playwright/test'
import { login, spillers } from './helpers'

// Admin > Security alerts against the real dev-mode app; the alert endpoints are stubbed with page.route.
const SHOTS = process.env.SHOTS_DIR ?? '' // screenshots only when asked for

const ALERTS = [
  { id: 3, kind: 'admin_burst', severity: 'high', subject_type: 'user', subject_id: '1', message: 'One admin actor made 12 identity or policy changes within 10 minutes.', evidence: { actions: 12, window_minutes: 10 }, first_seen: '2026-11-18T18:20:01', last_seen: '2026-11-18T18:27:40', count: 2, acknowledged_at: null, acknowledged_by: null },
  { id: 2, kind: 'login_pressure', severity: 'medium', subject_type: null, subject_id: null, message: 'Many failed logins from a few addresses.', evidence: { failures: 40 }, first_seen: '2026-11-17T10:00:00', last_seen: '2026-11-17T10:30:00', count: 1, acknowledged_at: null, acknowledged_by: null },
]
const ACKED = { ...ALERTS[1], id: 1, severity: 'low', acknowledged_at: '2026-11-18T09:00:00', acknowledged_by: 1 }

// Handlers check `live` so a request that lands after the test ended does not throw.
async function stub(page: Page, state: { open: typeof ALERTS; queries: string[]; acked: number[]; live: boolean }) {
  await page.route('**/admin/security-alerts**', async (r) => {
    if (!state.live) return r.abort()
    const u = new URL(r.request().url())
    if (u.pathname.endsWith('/summary')) return r.fulfill({ json: { open: state.open.length } })
    const ack = u.pathname.match(/\/(\d+)\/acknowledge$/)
    if (ack) {
      state.acked.push(Number(ack[1]))
      const hit = state.open.find((a) => a.id === Number(ack[1]))!
      state.open = state.open.filter((a) => a.id !== hit.id)
      return r.fulfill({ json: { ...hit, acknowledged_at: '2026-11-18T19:00:00', acknowledged_by: 1 } })
    }
    state.queries.push(u.search)
    return r.fulfill({ json: u.searchParams.get('status') === 'all' ? [...state.open, ACKED] : state.open })
  })
}

test('list, filter, acknowledge, health link, empty state', async ({ page }) => {
  const state = { open: [...ALERTS], queries: [] as string[], acked: [] as number[], live: true }
  try {
    await login(page)
    await stub(page, state)
    await page.goto('/admin?tab=security')
    const table = page.getByRole('table', { name: 'Security alerts' })
    const rows = table.locator('tbody tr')
    await expect(rows).toHaveCount(2)
    await expect(rows.nth(0)).toContainText('High')
    await expect(rows.nth(0)).toContainText('One admin actor made 12 identity or policy changes')
    await expect(rows.nth(0)).toContainText('actions: 12, window_minutes: 10')
    await expect(rows.nth(1)).toContainText('Medium')
    expect(state.queries[0]).toBe('?status=open')
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/alerts-desktop.png` })

    await page.getByLabel('Show acknowledged').check()
    await expect(rows).toHaveCount(3)
    expect(state.queries.at(-1)).toBe('?status=all')
    await expect(rows.nth(2)).toContainText('Acknowledged')
    await expect(rows.nth(2).getByRole('button')).toHaveCount(0)
    await page.getByLabel('Show acknowledged').uncheck()
    await expect(rows).toHaveCount(2)

    await rows.nth(0).getByRole('button', { name: 'Acknowledge' }).click()
    await expect.poll(() => state.acked).toEqual([3])
    await expect(rows).toHaveCount(1)
    await expect(page.getByRole('tab', { name: /Security alerts/ })).toContainText('1')

    await rows.nth(0).getByRole('button', { name: 'Acknowledge' }).click()
    await expect(page.getByText('No open security alerts.')).toBeVisible()
  } finally {
    state.live = false
  }
})

test('Health shows the open count and links to the tab', async ({ page }) => {
  const state = { open: [...ALERTS], queries: [] as string[], acked: [] as number[], live: true }
  try {
    await login(page)
    await stub(page, state)
    await page.route('**/admin/health', async (r) => {
      if (!state.live) return r.abort()
      if (r.request().resourceType() === 'document') return r.continue() // the SPA page itself lives at this path
      const res = await r.fetch()
      const body = await res.json()
      return r.fulfill({ response: res, json: { ...body, security_alerts_open: 2 } })
    })
    await page.goto('/admin/health')
    await page.getByRole('link', { name: '2 open security alerts' }).click()
    await expect(page).toHaveURL(/\/admin\?tab=security/)
    await expect(page.getByRole('table', { name: 'Security alerts' })).toBeVisible()
  } finally {
    state.live = false
  }
})

test('no sideways spill at 1440 and 390', async ({ page }) => {
  const state = { open: [...ALERTS], queries: [] as string[], acked: [] as number[], live: true }
  try {
    await login(page)
    await stub(page, state)
    for (const w of [1440, 390]) {
      await page.setViewportSize({ width: w, height: 900 })
      await page.goto('/admin?tab=security')
      await expect(page.getByRole('table', { name: 'Security alerts' })).toBeVisible()
      expect(await spillers(page), `at ${w}`).toEqual([])
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/alerts-${w}.png` })
    }
  } finally {
    state.live = false
  }
})
