import { test, expect } from '@playwright/test'
import { installMockBackend } from './fixtures'

async function openProject(page) {
  await page.goto('/')
  await page.getByText('Alpha', { exact: true }).first().click()
  await expect(page.getByText('Synthesis Workbench')).toBeVisible()
}

async function loadMetrics(mock, metrics) {
  await mock.frame({ type: 'project_state', metrics })
}

test('metric band stays dense: two rows of four at 46px with empty and negative values', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  // Negative timing values, a long cell count, and a missing power value in
  // one snapshot: no card may stretch the band or clip its own content.
  await loadMetrics(mock, {
    cellarea: 123.4, cells: 1234567, utilization: 61.25, power: null,
    setupslack: -12.345, holdslack: -0.004, fmax: 47.25, criticalpath: -21.2,
    target_mhz: 50, evidence_stage: 'screening STA', run_id: 'run-screen-01',
  })

  const cards = page.locator('.metric-card')
  await expect(cards).toHaveCount(8)
  const heights = await cards.evaluateAll((els) => els.map((el) => el.getBoundingClientRect().height))
  expect(new Set(heights.map((h) => Math.round(h)))).toEqual(new Set([46]))

  await expect(page.getByTestId('metric-setup-slack')).toContainText('-12.345')
  await expect(page.getByTestId('metric-hold-slack')).toContainText('-0.004')
  await expect(page.getByTestId('metric-estimated-power')).toContainText('—')

  // Two balanced rows of four; Fmax and critical delay sit in the second row
  // and are visible, never stranded.
  const tops = await cards.evaluateAll((els) => [...new Set(els.map((el) => Math.round(el.getBoundingClientRect().top)))])
  expect(tops.length).toBe(2)
  const secondRow = await page.evaluate(() => {
    const fmax = document.querySelector('[data-testid="metric-fmax"]').getBoundingClientRect()
    const critical = document.querySelector('[data-testid="metric-critical-delay"]').getBoundingClientRect()
    return { fmax: fmax.top, critical: critical.top, height: fmax.height }
  })
  expect(secondRow.fmax).toBeGreaterThan(tops[0])
  expect(secondRow.critical).toBe(secondRow.fmax)
  expect(secondRow.height).toBe(46)
})

test('long values never stretch a card or its row; the full value stays in the tooltip', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  await loadMetrics(mock, {
    cellarea: 12345678.912, cells: 9876543, utilization: 100.12345, power: 12.3456,
    setupslack: -123.4567, holdslack: 0.0412345, fmax: 1234.5678, criticalpath: 987.6543,
  })

  const container = page.locator('.metric-grid')
  const cards = page.locator('.metric-card')
  await container.evaluate((el) => { el.style.width = '760px' })
  await expect.poll(async () => (await cards.evaluateAll((els) => els.map((el) => el.getBoundingClientRect().top))).filter((t, i, a) => a.indexOf(t) === i).length).toBe(2)
  const heights = await cards.evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().height)))
  expect(heights.every((h) => h === 46)).toBe(true)
  await expect(page.getByTestId('metric-cell-area')).toHaveAttribute('title', /12,345,678\.9 um²/)

  // One column on a genuinely narrow container: still one compact line each.
  await container.evaluate((el) => { el.style.width = '340px' })
  await expect.poll(async () => (await cards.evaluateAll((els) => els.map((el) => el.getBoundingClientRect().left))).filter((x, i, a) => a.indexOf(x) === i).length).toBe(1)
  const narrowHeights = await cards.evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().height)))
  expect(narrowHeights.every((h) => h === 46)).toBe(true)
})

test('assurance stays a compact marker and missing metrics keep honest tooltips', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  // Empty state: every metric unavailable, nothing fabricated.
  await loadMetrics(mock, {})

  const cards = page.locator('.metric-card')
  await expect(cards).toHaveCount(8)
  await expect(page.getByTestId('metric-cells')).toContainText('—')
  await expect(page.getByTestId('metric-cells')).toHaveAttribute('title', /unavailable/)

  await loadMetrics(mock, { cells: 1024, power: 0.282 })
  const markers = await page.locator('.metric-card').evaluateAll((els) =>
    els.map((el) => ({ marker: el.querySelector('span[title]')?.getAttribute('title') ?? null, height: Math.round(el.getBoundingClientRect().height) })))
  const cellsCard = markers[1]
  expect(cellsCard.marker).toBe('screening')
  const powerCard = markers[3]
  expect(powerCard.marker).toBe('estimated')
  expect(markers.every((m) => m.height === 46)).toBe(true)
})
