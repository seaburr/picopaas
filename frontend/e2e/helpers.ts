import { expect, type Page } from '@playwright/test'
import { ADMIN } from './creds'

export async function login(page: Page) {
  await page.goto('/login')
  await page.getByLabel('Email').fill(ADMIN.email)
  await page.getByLabel('Password', { exact: true }).fill(ADMIN.password)
  await page.getByRole('button', { name: 'Log in' }).click()
  await expect(page).toHaveURL(/\/admin/)
}

// html and body clip horizontal overflow (style.css), so scrollWidth alone can not see a page that spills sideways.
// Instead list every visible element that ends past the viewport and is not inside its own horizontal scroller.
export function spillers(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const vw = window.innerWidth
    const out: string[] = []
    for (const el of Array.from(document.body.querySelectorAll('*'))) {
      const r = el.getBoundingClientRect()
      if (r.width === 0 || r.height === 0 || r.right <= vw + 1) continue
      let scroller = false
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        if (/(auto|scroll|hidden|clip)/.test(getComputedStyle(p).overflowX)) {
          scroller = true
          break
        }
      }
      if (!scroller) out.push(`${el.tagName.toLowerCase()}.${(el.getAttribute('class') ?? '').slice(0, 40)} right=${Math.round(r.right)} (viewport ${vw})`)
    }
    return out.slice(0, 5)
  })
}
