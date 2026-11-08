import { expect, test, type Page } from '@playwright/test'
import { login } from './helpers'

// Getting started dialog (once per browser session, until "Don't show this again"), plus round 2b copy changes.
const SHOTS = process.env.SHOTS_DIR
const shot = async (page: Page, name: string) => {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true })
}
const setDismissed = async (page: Page, v: boolean) =>
  expect((await page.request.patch('/auth/me', { data: { onboarding_dismissed: v } })).ok()).toBeTruthy()
const dialog = (page: Page) => page.getByRole('dialog', { name: 'Getting started' })

test.describe('getting started dialog', () => {
  test.afterEach(async ({ browser }) => {
    const ctx = await browser.newContext()
    const page = await ctx.newPage()
    await login(page)
    await setDismissed(page, true)
    await ctx.close()
  })

  test('shows after login, Dismiss hides it for the session, checkbox + Dismiss persists', async ({ page, browser }) => {
    await login(page)
    await setDismissed(page, false)
    await page.reload()
    await expect(dialog(page)).toBeVisible()
    await expect(dialog(page).getByRole('listitem')).toHaveCount(4)
    await shot(page, 'dialog')

    await page.keyboard.press('Escape')
    await expect(dialog(page)).toBeHidden()
    await page.goto('/account')
    await page.reload()
    await expect(page.getByRole('heading', { name: 'Account' })).toBeVisible()
    await expect(dialog(page)).toBeHidden() // same session: hidden

    await page.getByRole('button', { name: 'Show getting started' }).click()
    await expect(dialog(page)).toBeVisible()
    await page.getByRole('button', { name: 'Dismiss' }).click()
    await expect(dialog(page)).toBeHidden()

    // A new browser context is a new session: it shows again, and the checkbox makes it permanent.
    const ctx = await browser.newContext()
    const p2 = await ctx.newPage()
    await login(p2)
    await expect(dialog(p2)).toBeVisible()
    const patch = p2.waitForRequest((r) => r.method() === 'PATCH' && r.url().endsWith('/auth/me'))
    await dialog(p2).getByLabel("Don't show this again").check()
    await dialog(p2).getByRole('button', { name: 'Dismiss' }).click()
    expect((await patch).postDataJSON()).toEqual({ onboarding_dismissed: true })
    await expect(dialog(p2)).toBeHidden()
    await ctx.close()

    const ctx2 = await browser.newContext()
    const p3 = await ctx2.newPage()
    await login(p3)
    await p3.goto('/account')
    await expect(p3.getByRole('heading', { name: 'Account' })).toBeVisible()
    await expect(dialog(p3)).toBeHidden()
    await ctx2.close()
  })

  test('Deploy your first app opens Templates; not shown on public pages', async ({ page }) => {
    await page.goto('/guide/user')
    await expect(dialog(page)).toBeHidden()
    await login(page)
    await setDismissed(page, false)
    await page.reload()
    await dialog(page).getByRole('button', { name: 'Deploy your first app' }).click()
    await expect(page.getByRole('heading', { name: 'Templates' })).toBeVisible()
    await expect(dialog(page)).toBeHidden()
  })
})

test('templates page no longer says where new apps go', async ({ page }) => {
  await login(page)
  await page.goto('/templates')
  await expect(page.getByRole('heading', { name: 'Templates' })).toBeVisible()
  await expect(page.getByText('New apps from a template go in')).toHaveCount(0)
})

test('Kaisen empty page: How it works is Describe, Build, Deploy, then Spend', async ({ page }) => {
  await login(page)
  const list = (await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]
  await page.goto(`/projects/${list.find((p) => p.is_personal)!.id}/kaisen`)
  const rows = page.locator('table').filter({ hasText: '1. Describe' }).locator('tbody tr')
  await expect(rows.first()).toBeVisible()
  const heads = await rows.locator('th').allTextContents()
  expect(heads).toEqual(['1. Describe', '2. Build', '3. Deploy', 'Spend'])
})

for (const w of [1440, 1920]) {
  test(`docs prose fills the content column at ${w}px`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: 1000 })
    await page.goto('/guide/user')
    await page.locator('.doc > p').first().waitFor()
    const r = await page.evaluate(() => {
      const doc = document.querySelector('.doc')!
      const cs = getComputedStyle(doc)
      const inner = doc.getBoundingClientRect().right - parseFloat(cs.paddingRight)
      const longest = Array.from(doc.querySelectorAll(':scope > p, :scope > ul, :scope > blockquote'))
        .map((e) => e.getBoundingClientRect().right)
      return { inner, minRight: Math.min(...longest) }
    })
    expect(Math.abs(r.minRight - r.inner)).toBeLessThan(2)
    await shot(page, `docs-${w}`)
  })
}
