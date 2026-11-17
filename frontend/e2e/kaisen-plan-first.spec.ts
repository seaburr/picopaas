import { expect, test, type Page } from '@playwright/test'
import { login, spillers } from './helpers'

// "Confirm the plan first": New build checkbox, project default toggle, and the plan block on the build page. API stubbed.
const ID = 97
const SHOTS = process.env.SHOTS_DIR ?? ''
const PLAN = Array.from({ length: 90 }, (_, i) => `- [ ] Step ${i + 1}: add the thing that does something fairly long and descriptive, then check it in a very-long-unbroken-token-${'x'.repeat(60)}`).join('\n')
const settings = (planFirst: boolean, edit = true) => ({
  anthropic_key: { set: true, last4: 'wxyz' }, model: 'claude-haiku', effort: 'low', models: ['claude-haiku'], efforts: ['low'],
  reviewer: 'off', reviewer_model: 'claude-haiku', package_steward: 'on', plan_first: planFirst, cap_run: 1, cap_day: 5, cap_build: 3, github_token: { set: false, last4: null },
  web_access: false, web_project_enabled: true, web_allowed_domains: [], web_supported: true, web_models: [], web_note: '', web_search_usd: 0.01, web_domains_max: 20,
  can_edit: edit, updated_at: null,
})
const fulfill = (r: { fulfill: (o: { json: unknown; status?: number }) => Promise<void> }, json: unknown, status = 200) => r.fulfill({ json, status }).catch(() => undefined)

test('new build: checkbox follows the project default and is sent', async ({ page }) => {
  const bodies: Record<string, unknown>[] = []
  await page.route(/\/projects\/\d+\/kaisen(\?|$)/, (r) => (r.request().resourceType() === 'fetch' ? fulfill(r, settings(true)) : r.continue().catch(() => undefined)))
  await page.route('**/kaisen/builds', (r) => {
    if (r.request().method() !== 'POST') return r.continue().catch(() => undefined)
    bodies.push(r.request().postDataJSON())
    return fulfill(r, { detail: 'stop here' }, 400)
  })
  await login(page)
  const pid = ((await (await page.request.get('/projects')).json()) as { id: number; is_personal: boolean }[]).find((p) => p.is_personal)!.id
  await page.goto(`/projects/${pid}/kaisen/new`)
  const box = page.getByLabel('Confirm the plan first')
  await expect(box).toBeChecked()
  await expect(page.getByText('Kaisen writes a plan and waits for your go before it builds.')).toBeVisible()
  await box.uncheck()
  await page.getByLabel('Name', { exact: true }).fill('plan build')
  await page.locator('#kb-brief').fill('A todo list')
  await page.getByRole('button', { name: 'Start building' }).click()
  await expect.poll(() => bodies.length).toBe(1)
  expect(bodies[0].plan_first).toBe(false)
})

test('settings: default toggle saves', async ({ page }) => {
  const puts: Record<string, unknown>[] = []
  await page.route(/\/projects\/\d+\/kaisen(\?|$)/, (r) => {
    if (r.request().resourceType() !== 'fetch') return r.continue().catch(() => undefined)
    if (r.request().method() === 'PUT') { const b = r.request().postDataJSON(); puts.push(b); return fulfill(r, settings(b.plan_first === true)) }
    return fulfill(r, settings(false))
  })
  await page.route('**/projects/*/kaisen/members', (r) => fulfill(r, { project_id: 1, can_edit: true, members: [] }))
  await page.route('**/kaisen/spend**', (r) => fulfill(r, { last30_usd: 0 }))
  await login(page)
  await page.goto('/projects/1/kaisen/settings')
  const box = page.getByLabel('Confirm the plan first')
  await expect(box).not.toBeChecked()
  await box.check()
  await page.getByRole('button', { name: 'Save settings' }).click()
  await expect.poll(() => puts.length).toBe(1)
  expect(puts[0].plan_first).toBe(true)
})

function build(open: boolean): Record<string, unknown> {
  return {
    id: ID, name: 'Plan stub', status: open ? 'waiting' : 'running', app_id: null, app_hostname: null, app_status: null, file_count: 1, cost_usd: 0.1, plan_first: true,
    last_run_at: null, last_error: null, open_questions: open ? 1 : 0, created_by: { id: 1, email: 'e2e-admin@example.com' }, created_at: '2026-11-16T09:00:00Z',
    brief: 'x', project_id: 1, slug: 'plan-stub', files: [{ path: 'PLAN.md', size: 4000 }], runs: open ? [{ id: 1, kind: 'build', status: 'waiting', summary: null, error: null, cost_usd: 0.1, started_at: '2026-11-16T09:00:00Z', finished_at: null, transcript: null, transcript_trimmed: false }] : [], snapshots_kept: 10,
    inbox: [{ id: 5, kind: 'question', text: PLAN, status: open ? 'open' : 'answered', answer: open ? null : 'Go.', plan_confirm: true, created_at: '2026-11-16T09:00:00Z' }],
    secrets: [], spend: { build_usd: 0.1, cap_build: 5, month_usd: 0.1, last30_usd: 0.1 },
    output: { mode: 'download', repo: null, branch: null, push_on_finish: false, open_pr: false, auth: 'project', token: { set: false }, project_token_set: false, can_push: false },
    can_run: true, uploads: { count: 0, asset_count: 0, reference_count: 0, asset_bytes: 0, ref_bytes: 0, max_files: 20, asset_limit_bytes: 1e6, ref_limit_bytes: 1e6, changed_since_deploy: false, uploads: [], quota: {}, notice: '', uploads_enabled: true, uploads_project_enabled: true, uploads_platform_enabled: true, uploads_disabled_reason: null, uploads_not_enabled_label: '', uploads_notice: '' },
  }
}
async function stubBuild(page: Page, answers: Record<string, unknown>[]) {
  let answered = false
  await page.route(new RegExp(`/kaisen/builds/${ID}(/|\\?|$)`), (route) => {
    const req = route.request()
    if (req.resourceType() !== 'fetch' && req.resourceType() !== 'xhr') return route.continue().catch(() => undefined)
    const path = new URL(req.url()).pathname.replace(`/kaisen/builds/${ID}`, '')
    if (req.method() === 'GET' && path === '') return fulfill(route, build(!answered))
    if (req.method() === 'POST' && path === '/inbox/5/answer') { answers.push(req.postDataJSON()); answered = true; return fulfill(route, build(false)) }
    if (path === '/uploads') return fulfill(route, build(true).uploads)
    return fulfill(route, [])
  })
  await page.route('**/projects/*/kaisen', (r) => fulfill(r, settings(true)))
}

for (const w of [1280, 390]) {
  test(`plan to confirm at ${w}px: shown, no overflow, Build it sends Go.`, async ({ page }) => {
    const answers: Record<string, unknown>[] = []
    await page.setViewportSize({ width: w, height: 900 })
    await login(page)
    await stubBuild(page, answers)
    await page.goto(`/kaisen/${ID}`)
    await expect(page.getByRole('heading', { name: 'Plan to confirm' })).toBeVisible()
    await expect(page.getByTestId('plan-text')).toContainText('Step 1:')
    await expect(page.getByLabel('Or say what to change')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Stop run' })).toBeVisible()
    expect(await spillers(page)).toEqual([])
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/plan-${w}.png`, fullPage: true })
    await page.getByRole('button', { name: 'Build it' }).click()
    await expect.poll(() => answers.length).toBe(1)
    expect(answers[0].text).toBe('Go.')
    await expect(page.getByRole('heading', { name: 'Plan', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Build it' })).toHaveCount(0)
  })
}
