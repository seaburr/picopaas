import { expect, test } from '@playwright/test'
import { ADMIN } from './creds'

// The light theme is a profile setting (PATCH /auth/me) cached in localStorage for first paint. Dark is the default.
const bg = (page: import('@playwright/test').Page) =>
  page.evaluate(() => getComputedStyle(document.body).backgroundColor)

test('theme: dark by default, light applies immediately and persists across reload', async ({ page }) => {
  await test.step('logged-out pages are dark by default', async () => {
    await page.goto('/login')
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    expect(await bg(page)).toBe('rgb(18, 20, 23)')
  })

  await page.getByLabel('Email').fill(ADMIN.email)
  await page.getByLabel('Password', { exact: true }).fill(ADMIN.password)
  await page.getByRole('button', { name: 'Log in' }).click()
  await expect(page).toHaveURL(/\/admin/)

  await page.goto('/account')
  const group = page.getByRole('radiogroup', { name: 'Theme' })
  await expect(group.getByRole('radio', { name: 'Dark' })).toHaveAttribute('aria-checked', 'true')

  await test.step('choosing Light applies at once', async () => {
    await group.getByRole('radio', { name: 'Light' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
    expect(await bg(page)).toBe('rgb(241, 238, 230)')
  })

  await test.step('it survives a reload (cache), before Vue boots', async () => {
    await page.reload()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
    await expect(group.getByRole('radio', { name: 'Light' })).toHaveAttribute('aria-checked', 'true')
  })

  await test.step('the server value wins when the cache is gone', async () => {
    await page.evaluate(() => localStorage.removeItem('picopaas-theme'))
    await page.reload()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  })

  await test.step('arrow keys switch back to Dark and save it', async () => {
    await group.getByRole('radio', { name: 'Light' }).focus()
    await page.keyboard.press('ArrowLeft')
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    await page.evaluate(() => localStorage.removeItem('picopaas-theme'))
    await page.reload()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    expect(await bg(page)).toBe('rgb(18, 20, 23)')
  })
})
