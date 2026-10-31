import { request } from '@playwright/test'
import { ADMIN } from './creds'

// Seed through the supported path: the first account registered on an empty DB is the active platform admin.
export default async function globalSetup() {
  const ctx = await request.newContext({ baseURL: 'http://127.0.0.1:8765' })
  const res = await ctx.post('/auth/register', { data: { ...ADMIN, accept_terms: true } })
  if (res.status() !== 201) throw new Error(`seed register failed: ${res.status()} ${await res.text()}`)
  await ctx.dispose()
}
