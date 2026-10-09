import { test, expect } from '@playwright/test'
import { installMockBackend } from './fixtures'

async function openProject(page, name = 'Alpha') {
  await page.goto('/')
  await page.getByText(name, { exact: true }).first().click()
  await expect(page.getByText('Synthesis Workbench')).toBeVisible()
}

test('keeps live job/log state across delayed REST without yanking the selected surface', async ({ page }) => {
  const mock = installMockBackend(page, { delayedJobs: true })
  await openProject(page)

  await mock.jobStarted()
  await expect(page.getByTestId('active-job-strip')).toBeVisible()
  await expect(page.getByText('compile started')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Sources', exact: true })).toBeVisible()
  await page.getByTestId('active-job-strip').click()
  await expect(page.getByText('compile started')).toBeVisible()
  await mock.log('job-live', 3, 'still running')
  await expect(page.getByText('still running')).toBeVisible()

  // The delayed registry response must not rewind the live job or its lines.
  mock.releaseJobs()
  await expect(page.getByText('still running')).toBeVisible()
  await expect(page.getByText('running', { exact: true })).toBeVisible()
  await expect(page.getByRole('main').getByText('simulation', { exact: true })).toBeVisible()

  // A foreign broadcast remains global but is hidden by the project filter.
  await mock.send({ type: "job_started", job_id: "job-foreign", kind: "synthesis", design: "other", session: "proj-b" })
  await expect(page.getByText('job-foreign')).toHaveCount(0)
  await page.getByRole('button', { name: 'scope: this project' }).click()
  await expect(page.getByText('job-foreign')).toBeVisible()
  await page.getByRole('button', { name: 'scope: all jobs' }).click()
  await expect(page.getByText('job-foreign')).toHaveCount(0)

  // Cache activity is rendered as activity, with no synthetic job row.
  await mock.result()
  await expect(page.getByText('cached', { exact: true })).toBeVisible()
  await expect(page.getByText('job-live')).toBeVisible()
  await expect(page.getByRole('button', { name: /Jobs \(1\)/ })).toBeVisible()
  await expect(page.getByText('job-foreign')).toHaveCount(0)
})

test('queues cancellation through reconnect and keeps project filter/autoswitch deterministic', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  await mock.jobStarted()
  await page.getByTestId('active-job-strip').click()
  await expect(page.getByText('Jobs (1)')).toBeVisible()

  await mock.closeSocket()
  await page.getByTitle('Cancel job (kills the process tree)').click()
  await expect.poll(async () => await mock.wasSent("cancel_job", "job-live")).toBe(false)
  await expect.poll(() => page.evaluate(() => window.__mockSockets.length)).toBeGreaterThan(1)
  await expect.poll(async () => await mock.wasSent("cancel_job", "job-live")).toBe(true)

  // Switching projects resets the local scope and does not expose Alpha's job.
  await page.getByRole('button', { name: '← Projects' }).click()
  await page.getByText('Beta', { exact: true }).click()
  await expect(page.getByText('job-live')).toHaveCount(0)
})

test('pauses and resumes log following; finished rows expand and request a replay', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  await mock.jobStarted()
  await page.getByTestId('active-job-strip').click()
  await expect(page.getByText('compile started')).toBeVisible()

  const log = page.getByTestId('job-log-job-live')
  for (let seq = 3; seq <= 45; seq += 1) await mock.log('job-live', seq, `filler ${seq}`)
  await expect(page.getByText('filler 45')).toBeVisible()
  await expect.poll(() => log.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true)
  await log.evaluate((el) => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')) })
  await mock.log('job-live', 46, 'after pause')
  await expect(page.getByText('after pause')).toBeVisible()
  await expect.poll(() => log.evaluate((el) => el.scrollTop)).toBe(0)

  await log.evaluate((el) => { el.scrollTop = el.scrollHeight; el.dispatchEvent(new Event('scroll')) })
  await mock.log('job-live', 47, 'after resume')
  await expect(page.getByText('after resume')).toBeVisible()
  await expect.poll(() => log.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(12)

  await mock.finish()
  await expect(page.getByText('done')).toBeVisible()
  await page.getByText('job-live').click()
  await expect.poll(() => page.evaluate(() => (window.__mockSent || []).some((f) => f.type === "subscribe" && f.job_id === "job-live" && f.since_seq === 0))).toBe(true)
  await expect(page.getByTestId('job-log-job-live')).toBeVisible()

  // Updating another row must not collapse/remount the expanded finished row.
  await mock.send({ type: "job_started", job_id: "job-second", kind: "synthesis", design: "other", session: "proj-a" })
  await mock.send({ type: "job_status", job_id: "job-second", status: "running" })
  await expect(page.getByText('job-second')).toBeVisible()
  await expect(page.getByTestId('job-log-job-live')).toBeVisible()
})
