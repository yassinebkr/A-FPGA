import { defineConfig, devices } from '@playwright/test'

const perfQualification = process.env.PLAYWRIGHT_PERF === '1'

export default defineConfig({
  testDir: './tests/browser',
  fullyParallel: false,
  // The large-netlist stress measurement is intentionally isolated. The
  // normal suite keeps all functional C5 coverage while avoiding noisy heap
  // and frame timings from unrelated parallel browser pages.
  grep: perfQualification ? /@perf/ : undefined,
  grepInvert: perfQualification ? undefined : /@perf/,
  workers: perfQualification ? 1 : undefined,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
  },
  webServer: {
    command: './node_modules/.bin/vite --host 127.0.0.1 --port 4173',
    cwd: '.',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
})
