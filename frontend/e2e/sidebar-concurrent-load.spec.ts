import { expect, test } from '@playwright/test'
import { login } from './helpers'

// Several projects open in the sidebar load their apps at the same time; each must keep its own list. (The store used to
// snapshot the cache before awaiting, so every project but the last to answer fell back to "No apps".)
const app = (id: number, pid: number, name: string) => ({
  id, name, project_id: pid, hostname: `a${id}.picopaas.xyz`, status: 'running', status_detail: null, env: [],
  image_history: [], kaisen_build_id: null,
})

test('projects loading together each keep their apps', async ({ page }) => {
  await login(page)
  const real = (await (await page.request.get('/projects')).json()) as Record<string, unknown>[]
  const base = real.find((p) => p.is_personal)!
  const extra = [901, 902].map((id, i) => ({ ...base, id, is_personal: false, name: `Side ${i + 1}`, slug: `side-${i + 1}`, app_count: 1 }))
  const isApi = (t: string) => t === 'fetch' || t === 'xhr'
  await page.route(/\/projects$/, (r) =>
    isApi(r.request().resourceType()) && r.request().method() === 'GET' ? r.fulfill({ json: [...real, ...extra] }) : r.continue())
  // The first project opened answers last.
  await page.route(/\/apps\?project_id=90[12]$/, async (r) => {
    if (!isApi(r.request().resourceType())) return r.continue()
    const pid = Number(new URL(r.request().url()).searchParams.get('project_id'))
    await new Promise((res) => setTimeout(res, pid === 901 ? 700 : 100))
    return r.fulfill({ json: [app(pid * 10, pid, `app-of-${pid}`)] })
  })
  await page.goto('/projects')
  for (const n of ['Side 1', 'Side 2']) await page.locator('.side .tree summary', { hasText: n }).first().click({ position: { x: 5, y: 12 } })
  await expect(page.locator('.side .tree a:visible', { hasText: 'app-of-901' })).toBeVisible()
  await expect(page.locator('.side .tree a:visible', { hasText: 'app-of-902' })).toBeVisible()
})
