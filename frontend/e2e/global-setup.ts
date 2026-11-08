import { request } from '@playwright/test'
import { ADMIN } from './creds'

// Seed through the supported path: the first account registered on an empty DB is the active platform admin.
export default async function globalSetup() {
  const ctx = await request.newContext({ baseURL: `http://127.0.0.1:${process.env.E2E_PORT ?? 8765}` })
  const res = await ctx.post('/auth/register', { data: { ...ADMIN, accept_terms: true } })
  if (res.status() !== 201) throw new Error(`seed register failed: ${res.status()} ${await res.text()}`)
  // The shared admin has seen the Getting started dialog, so it does not cover every other spec (onboarding.spec.ts resets it).
  const me = await ctx.patch('/auth/me', { data: { onboarding_dismissed: true } })
  if (!me.ok()) throw new Error(`onboarding dismiss failed: ${me.status()} ${await me.text()}`)
  await ctx.dispose()
}
