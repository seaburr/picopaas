import { existsSync } from 'node:fs'
import { resolve } from 'node:path'
import { defineConfig, devices } from '@playwright/test'

// Smoke test against the real app: FastAPI in dev mode (SQLite, reconciler off) serving the built frontend
// from ../static. Run `npm run build` first. Python: E2E_PYTHON, else ../.venv, else `python` (CI).
const PORT = 8765
const python = process.env.E2E_PYTHON ?? (existsSync('../.venv/bin/python') ? resolve('../.venv/bin/python') : 'python')
const tmpDir = resolve('e2e/.tmp')
const dbFile = `${tmpDir}/e2e.db`

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
    ...devices['Desktop Chrome'],
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    // Fresh DB every run; the first registered user becomes the platform admin (app/routers/auth.py).
    command: `mkdir -p ${tmpDir} && rm -f ${dbFile} && ${python} -m uvicorn app.main:app --host 127.0.0.1 --port ${PORT}`,
    cwd: '..',
    url: `http://127.0.0.1:${PORT}/healthz`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      ENVIRONMENT: 'dev',
      DATABASE_URL: `sqlite:///${dbFile}`,
      RECONCILER_ENABLED: '0',
      KAISEN_ENABLED: '1',
    },
  },
})
