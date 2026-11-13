import { expect, test } from '@playwright/test'
import { login } from './helpers'

// Real dev backend, no Kaisen route stubs: draft build (start:false, no model call) -> Fork dialog -> fork notice -> delete both.
const SHOTS = process.env.SHOTS_DIR

test('draft build: fork it, see the fork notice, delete the fork and the source', async ({ page }) => {
  await login(page)
  const pid = ((await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]).find((p) => p.is_personal)!.id
  const name = `Real ${Date.now()}`
  const made = await page.request.post('/kaisen/builds', { data: { project_id: pid, name, brief: 'A small page that says hello.', start: false } })
  expect(made.status(), await made.text()).toBe(201)
  const src = (await made.json()) as { id: number }

  await page.goto(`/kaisen/${src.id}`)
  await expect(page.getByRole('heading', { name })).toBeVisible()

  await page.getByRole('button', { name: 'Fork this build' }).click()
  const d = page.getByRole('dialog', { name: 'Fork this build' })
  await expect(d).toBeVisible()
  await expect(d.getByLabel('Name')).toHaveValue(`${name} fork`)
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/real-fork-dialog.png` })
  await d.getByRole('button', { name: 'Fork', exact: true }).click()

  await expect(page).toHaveURL(new RegExp(`/kaisen/(?!${src.id}$)\\d+$`))
  await expect(page.getByRole('heading', { name: `${name} fork` })).toBeVisible()
  const notice = page.getByTestId('fork-notice')
  await expect(notice).toContainText(`Forked from ${name}`)
  await expect(notice).toContainText('current files')
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/real-fork-notice.png` })

  async function remove(title: string) {
    await page.getByRole('button', { name: 'Delete build' }).click()
    const c = page.getByRole('dialog', { name: 'Delete build?' })
    await c.getByRole('textbox').fill(title)
    await c.getByRole('button', { name: 'Delete build' }).click()
    await expect(page).toHaveURL(/\/kaisen$/)
  }
  await remove(`${name} fork`)

  await page.goto(`/kaisen/${src.id}`)
  await expect(page.getByRole('heading', { name })).toBeVisible()
  await remove(name)
  expect((await page.request.get(`/kaisen/builds/${src.id}`)).status()).toBe(404)
})
