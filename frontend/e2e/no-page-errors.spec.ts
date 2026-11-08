import { expect, test } from '@playwright/test'
import { login } from './helpers'

// Every main page must render without an uncaught error. A setup-order bug (a composable reading a value declared
// later) crashed /projects/:pid/kaisen/new into "Something went wrong" while specs that only checked the URL passed.
test('main pages load without uncaught errors', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`${page.url()}: ${e.message}`))
  await login(page)
  const list = (await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]
  const pid = list.find((p) => p.is_personal)!.id
  const paths = [
    `/projects/${pid}/apps`, `/projects/${pid}/apps/new`, `/projects/${pid}/apps/new?from=image`,
    `/projects/${pid}/kaisen`, `/projects/${pid}/kaisen/new`, `/projects/${pid}/kaisen/settings`,
    '/projects', '/templates', '/guide', '/account', '/admin', '/admin/health',
  ]
  for (const p of paths) {
    await page.goto(p)
    await expect(page.locator('#main'), p).toBeVisible()
    await expect(page.getByRole('heading', { level: 1, name: 'Something went wrong' }), p).toHaveCount(0)
  }
  expect(errors).toEqual([])
})
