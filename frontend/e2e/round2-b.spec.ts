import { expect, test } from '@playwright/test'
import { login, spillers } from './helpers'

// Feedback round 2, batch B: empty Kaisen page, one test message, project step in the template flow.
const SHOTS = process.env.SHOTS_DIR
const shot = async (page: import('@playwright/test').Page, name: string) => {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true })
}
const sizes = [{ w: 1440, h: 900 }, { w: 390, h: 844 }]

async function projects(page: import('@playwright/test').Page, extra?: string) {
  const list = (await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]
  const personal = list.find((p) => p.is_personal)!.id
  let made: { id: number; name: string } | null = null
  if (extra) {
    const r = await page.request.post('/projects', { data: { name: extra } })
    expect(r.status()).toBe(201)
    made = (await r.json()) as { id: number; name: string }
  }
  return { personal, made }
}

for (const s of sizes) {
  test.describe(`at ${s.w}px`, () => {
    test.use({ viewport: { width: s.w, height: s.h }, colorScheme: 'dark' })

    test('empty Kaisen page uses the width and has no project switcher', async ({ page }) => {
      await login(page)
      const { personal, made } = await projects(page, `E2E Switcher ${Date.now()}`)
      try {
        await page.goto(`/projects/${personal}/kaisen`)
        await expect(page.getByRole('heading', { name: 'Build your first app with Kaisen' })).toBeVisible()
        await expect(page.locator('#main').getByText('Other projects')).toHaveCount(0)
        await expect(page.getByRole('heading', { name: 'How it works' })).toBeVisible()
        expect(await spillers(page)).toEqual([])
        await shot(page, `kaisen-empty-${s.w}`)
      } finally {
        await page.request.delete(`/projects/${made!.id}`)
      }
    })

    test('Kaisen settings test shows only the inline message', async ({ page }) => {
      await login(page)
      const { personal } = await projects(page)
      await page.route('**/projects/*/kaisen/test/anthropic', (r) => r.fulfill({ json: { ok: true, message: 'Anthropic key works.' } }))
      await page.goto(`/projects/${personal}/kaisen/settings`)
      await page.getByRole('button', { name: 'Test Anthropic' }).click()
      await expect(page.getByText('Anthropic key works.')).toHaveCount(1)
      await expect(page.locator('.alert-success', { hasText: 'Anthropic key works.' })).toBeVisible()
      await shot(page, `kaisen-settings-test-${s.w}`)
    })

    test('template flow asks for the project and deploys into the chosen one', async ({ page }) => {
      await login(page)
      const { personal, made } = await projects(page, `E2E Tpl ${Date.now()}`)
      try {
        await page.goto(`/templates?project=${personal}`)
        await page.getByRole('button', { name: 'Use this template' }).last().click()
        const sel = page.getByLabel('Project')
        await expect(sel).toBeVisible()
        await expect(sel).toBeInViewport()
        await expect(sel).toBeFocused()
        await expect(sel).toHaveValue(String(personal))
        await shot(page, `template-project-step-${s.w}`)
        await page.keyboard.press('Escape')
        await expect(sel).toHaveCount(0)
        await expect(page.getByRole('button', { name: 'Use this template' }).last()).toBeFocused()
        await page.getByRole('button', { name: 'Use this template' }).last().click()
        await sel.selectOption(String(made!.id))
        await page.getByRole('button', { name: 'Continue' }).click()
        await expect(page).toHaveURL(new RegExp(`/projects/${made!.id}/apps/new\\?template=`))
        await expect(page.getByRole('heading', { level: 1 })).toContainText(`New app in ${made!.name}`)
      } finally {
        await page.request.delete(`/projects/${made!.id}`)
      }
    })
  })
}
