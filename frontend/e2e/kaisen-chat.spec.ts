import { expect, test, type Page, type Route } from '@playwright/test'
import { login } from './helpers'

// The Kaisen build page without a cluster or a real build: every /kaisen/builds/99 call is answered from a stub.
// Only the API is stubbed; the page, composer, scrolling and polling are the real app.
const ID = 99
type Json = Record<string, unknown>

function run(n: number, extra: Json = {}): Json {
  return {
    id: n, kind: n === 1 ? 'build' : 'feedback', status: 'done', summary: `Run ${n}: ${'Kaisen changed some files and checked the result. '.repeat(4)}`,
    error: null, cost_usd: 0.01, started_at: `2026-11-05T10:${String(n % 60).padStart(2, '0')}:00Z`, finished_at: `2026-11-05T10:${String(n % 60).padStart(2, '0')}:30Z`,
    transcript: [{ role: 'assistant', text: 'Done.' }], transcript_trimmed: false, ...extra,
  }
}
function build(runs: Json[], inbox: Json[] = [], extra: Json = {}): Json {
  return {
    id: ID, name: 'Chat stub', status: 'done', app_id: null, app_hostname: null, app_status: null, file_count: 0, cost_usd: 0.1, last_run_at: null, last_error: null,
    open_questions: inbox.filter((i) => i.status === 'open').length, created_by: { id: 1, email: 'e2e-admin@example.com' }, created_at: '2026-11-05T09:00:00Z',
    brief: 'Build me a small page.', project_id: 1, slug: 'chat-stub', files: [], runs, inbox,
    spend: { build_usd: 0.1, cap_build: 5, month_usd: 0.1, last30_usd: 0.1 },
    output: { mode: 'download', repo: null, branch: null, push_on_finish: false, open_pr: false, auth: 'project', token: { set: false }, project_token_set: false, can_push: false },
    can_run: true, ...extra,
  }
}
const uploadsOk = { uploads: [], quota: {}, notice: '', uploads_enabled: true, uploads_project_enabled: true, uploads_platform_enabled: true, uploads_disabled_reason: null, uploads_not_enabled_label: '', uploads_notice: '' }
const summaryOk = { count: 0, asset_count: 0, reference_count: 0, asset_bytes: 0, ref_bytes: 0, max_files: 20, asset_limit_bytes: 1e6, ref_limit_bytes: 1e6, changed_since_deploy: false, ...uploadsOk }

interface Stub { calls: { method: string; path: string; body: unknown }[]; state: { get: () => Json } }
// state.get() is called for every GET of the build, so a test can make the build grow between polls.
async function stub(page: Page, get: () => Json, post?: (path: string, body: unknown) => Json | null): Promise<Stub> {
  const calls: Stub['calls'] = []
  await page.route(new RegExp(`/kaisen/builds/${ID}(/|\\?|$)`), async (route: Route) => {
    const req = route.request()
    if (req.resourceType() !== 'fetch' && req.resourceType() !== 'xhr') return route.continue()
    const path = new URL(req.url()).pathname.replace(`/kaisen/builds/${ID}`, '')
    const body = req.postData() ? JSON.parse(req.postData() as string) : null
    calls.push({ method: req.method(), path, body })
    if (req.method() === 'GET' && path === '') return route.fulfill({ json: { ...get(), uploads: summaryOk } })
    if (req.method() === 'GET' && path === '/uploads') return route.fulfill({ json: uploadsOk })
    if (req.method() === 'GET') return route.fulfill({ json: [] })
    const out = post?.(path, body)
    return route.fulfill({ json: { ...(out ?? get()), uploads: summaryOk } })
  })
  // settings (cost hint) is optional and the page copes without it
  await page.route('**/projects/*/kaisen', (r) => r.fulfill({ status: 404, json: { detail: 'none' } }))
  return { calls, state: { get } }
}

const composer = (page: Page) => page.getByLabel(/Tell Kaisen what to change|Your answer to Kaisen/)
const scroller = (page: Page) => page.getByRole('region', { name: 'Conversation messages' })
const scrollInfo = (page: Page) => scroller(page).evaluate((el) => ({ top: el.scrollTop, gap: el.scrollHeight - el.scrollTop - el.clientHeight, max: el.scrollHeight - el.clientHeight }))

test.describe('composer', () => {
  test('no open question: Enter is a newline, Ctrl and Cmd+Enter and the button send feedback', async ({ page }) => {
    await login(page)
    const s = await stub(page, () => build([run(1)]))
    await page.goto(`/kaisen/${ID}`)
    const box = composer(page)
    await expect(box).toBeVisible()
    const posts = () => s.calls.filter((c) => c.method === 'POST')

    await box.fill('first line')
    await box.press('Enter')
    await box.pressSequentially('second line')
    await expect(box).toHaveValue('first line\nsecond line')
    await page.waitForTimeout(300)
    expect(posts(), 'Enter alone must not send').toHaveLength(0)

    await box.press('Control+Enter')
    await expect.poll(() => posts().length).toBe(1)
    expect(posts()[0].path).toBe('/feedback')
    expect(posts()[0].body).toEqual({ text: 'first line\nsecond line' })
    await expect(box).toHaveValue('')

    await box.fill('from the Mac')
    await box.press('Meta+Enter')
    await expect.poll(() => posts().length).toBe(2)
    expect(posts()[1]).toMatchObject({ path: '/feedback', body: { text: 'from the Mac' } })

    await box.fill('from the button')
    await page.getByRole('button', { name: 'Send to Kaisen' }).click()
    await expect.poll(() => posts().length).toBe(3)
    expect(posts()[2]).toMatchObject({ path: '/feedback', body: { text: 'from the button' } })
    expect(posts().every((c) => c.path === '/feedback')).toBe(true)
  })

  test('open question: sending answers it (not feedback) and attaching is off with its reason', async ({ page }) => {
    await login(page)
    const q = { id: 7, kind: 'question', text: 'Which colour should the header be?', status: 'open', answer: null, created_at: '2026-11-05T10:05:00Z' }
    const s = await stub(page, () => build([run(1)], [q]), (path) => (path.includes('/answer') ? build([run(1)], [{ ...q, status: 'answered', answer: 'blue' }]) : null))
    await page.goto(`/kaisen/${ID}`)
    const box = composer(page)
    await expect(page.getByText('Answering Kaisen\'s question')).toBeVisible()
    await expect(box).toBeVisible()
    // attaching is disabled, and the reason is on screen
    await expect(page.getByRole('button', { name: 'Attach' })).toHaveAttribute('aria-disabled', 'true')
    await expect(page.getByText("Files can't go with an answer. Attach them to your next message.")).toBeVisible()

    await box.fill('blue')
    await box.press('Enter')
    await page.waitForTimeout(300)
    expect(s.calls.filter((c) => c.method === 'POST')).toHaveLength(0)

    await box.press('Control+Enter')
    await expect.poll(() => s.calls.filter((c) => c.method === 'POST').length).toBe(1)
    const post = s.calls.filter((c) => c.method === 'POST')[0]
    expect(post.path).toBe('/inbox/7/answer')
    expect(post.body).toEqual({ text: 'blue' })
    expect(s.calls.some((c) => c.path === '/feedback')).toBe(false)
  })

  test('an answer in flight locks the composer, so the old text can not go to the next question', async ({ page }) => {
    await login(page)
    const q1 = { id: 7, kind: 'question', text: 'First question?', status: 'open', answer: null, created_at: '2026-11-05T10:05:00Z' }
    const q2 = { id: 8, kind: 'question', text: 'Second question?', status: 'open', answer: null, created_at: '2026-11-05T10:06:00Z' }
    let release = () => {}
    const gate = new Promise<void>((res) => (release = res))
    let first = true
    const s = await stub(page, () => (first ? build([run(1)], [q1, q2]) : build([run(1)], [{ ...q1, status: 'answered', answer: 'one' }, q2])))
    // Hold the answer request open. (An idle build is not polled, so the mid-flight refresh that moved `answering` on
    // can not be reproduced from the page; this covers the lock itself: disabled composer, no second send.)
    await page.route(new RegExp(`/kaisen/builds/${ID}/inbox/7/answer`), async (r) => {
      await gate
      return r.fulfill({ json: { ...build([run(1)], [{ ...q1, status: 'answered', answer: 'one' }, q2]), uploads: summaryOk } })
    })
    await page.goto(`/kaisen/${ID}`)
    const box = composer(page)
    await box.fill('one')
    await box.press('Control+Enter')
    await expect(page.getByRole('button', { name: 'Sending…' })).toBeDisabled()
    await expect(box).toBeDisabled()
    first = false
    // a second send attempt while in flight goes nowhere
    await box.evaluate((el: HTMLTextAreaElement) => el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, bubbles: true })))
    release()
    await expect(page.getByText(/Second question\?/).first()).toBeVisible()
    const answers = s.calls.filter((c) => c.path.includes('/answer'))
    expect(answers.length).toBeLessThanOrEqual(1)
    expect(s.calls.some((c) => c.path === '/inbox/8/answer')).toBe(false)
    await expect(box).toHaveValue('')
  })
})

test.describe('chat scroll', () => {
  test.use({ viewport: { width: 1280, height: 720 } })
  const many = (n: number) => Array.from({ length: n }, (_, i) => run(i + 1))

  test('opens at the bottom; new content stays pinned; scrolled up it does not move and Latest works; Run details does not jump', async ({ page }) => {
    await login(page)
    // A running build is polled every 3 s. Each GET adds one run, so the thread keeps growing under the reader.
    let n = 40
    const s = await stub(page, () => build(many(n), [], { status: 'running' }))
    const grow = () => {
      n += 1
    }
    await page.goto(`/kaisen/${ID}`)
    await expect(scroller(page)).toBeVisible()
    await expect.poll(async () => (await scrollInfo(page)).max, { message: 'thread must overflow to test scrolling' }).toBeGreaterThan(300)

    await test.step('opens at the bottom', async () => {
      await expect.poll(async () => (await scrollInfo(page)).gap).toBeLessThan(4)
      await expect(page.getByRole('button', { name: 'Jump to latest message' })).toHaveCount(0)
    })

    await test.step('new content keeps it pinned', async () => {
      const before = (await scrollInfo(page)).max
      grow()
      await expect.poll(async () => (await scrollInfo(page)).max, { timeout: 8000 }).toBeGreaterThan(before)
      await expect.poll(async () => (await scrollInfo(page)).gap).toBeLessThan(4)
    })

    await test.step('after scrolling up, new content does not move it and Latest appears', async () => {
      await scroller(page).evaluate((el) => el.scrollTo({ top: 120 }))
      await expect(page.getByRole('button', { name: 'Jump to latest message' })).toBeVisible()
      const before = await scrollInfo(page)
      grow()
      const getsBefore = s.calls.filter((c) => c.method === 'GET' && c.path === '').length
      await expect.poll(() => s.calls.filter((c) => c.method === 'GET' && c.path === '').length, { timeout: 8000 }).toBeGreaterThan(getsBefore)
      await page.waitForTimeout(500)
      const after = await scrollInfo(page)
      expect(after.max).toBeGreaterThan(before.max) // it did grow
      expect(Math.abs(after.top - before.top)).toBeLessThanOrEqual(2) // and the reader did not move
      await expect(page.getByRole('button', { name: 'Jump to latest message' })).toBeVisible()
    })

    await test.step('Latest scrolls to the end and re-pins', async () => {
      await page.getByRole('button', { name: 'Jump to latest message' }).click()
      await expect.poll(async () => (await scrollInfo(page)).gap, { timeout: 5000 }).toBeLessThan(4)
      await expect(page.getByRole('button', { name: 'Jump to latest message' })).toHaveCount(0)
    })

    await test.step('opening Run details does not jump', async () => {
      await scroller(page).evaluate((el) => el.scrollTo({ top: el.scrollHeight / 2 }))
      await page.waitForTimeout(200)
      const before = await scrollInfo(page)
      await scroller(page).getByText('Run details').nth(20).scrollIntoViewIfNeeded()
      const mid = await scrollInfo(page)
      await scroller(page).getByText('Run details').nth(20).click()
      await page.waitForTimeout(500)
      const after = await scrollInfo(page)
      expect(before.top).toBeGreaterThan(0)
      expect(Math.abs(after.top - mid.top), 'opening a disclosure must not move the reader').toBeLessThanOrEqual(2)
    })
  })

  test('Run details at the bottom of a pinned thread does not jump either', async ({ page }) => {
    await login(page)
    await stub(page, () => build(many(30)))
    await page.goto(`/kaisen/${ID}`)
    await expect.poll(async () => (await scrollInfo(page)).gap).toBeLessThan(4)
    const last = scroller(page).getByText('Run details').last()
    const before = await scrollInfo(page)
    await last.click()
    await page.waitForTimeout(500)
    const after = await scrollInfo(page)
    expect(Math.abs(after.top - before.top)).toBeLessThanOrEqual(2)
  })
})
