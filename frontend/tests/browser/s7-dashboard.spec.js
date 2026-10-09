import { test, expect } from '@playwright/test'
import { installMockBackend } from './fixtures'

async function openProject(page, name = 'Alpha') {
  await page.goto('/')
  await page.getByText(name, { exact: true }).first().click()
  await expect(page.getByText('Synthesis Workbench')).toBeVisible()
}

async function loadMetrics(mock) {
  await mock.frame({ type: 'project_state', metrics: {
    cellarea: 0, totalarea: 1234.5, utilization: 61.25, cells: 1024,
    power: 0.282, setupslack: -0.12, holdslack: 0.04, fmax: 47.25,
    target_mhz: 50, evidence_stage: 'screening STA',
    run_id: 'run-screen-01', candidate: 'cand-r5',
    provenance: { comparison: { label: 'measured best' } },
  } })
}

test('renders eight ordered metrics with missing and zero values plus provenance', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  await loadMetrics(mock)
  const cards = page.locator('.metric-card')
  await expect(cards).toHaveCount(8)
  await expect(page.getByTestId('metric-cell-area')).toContainText('0')
  await expect(page.getByTestId('metric-hold-slack')).toContainText('0.04')
  await expect(page.getByTestId('metric-provenance')).toContainText('run-screen-01')
  await expect(page.getByText('target 50 MHz')).toBeVisible()
})

test('uses container width for 4x2, 2x4, and 1x8 metric layouts', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  await loadMetrics(mock)
  const container = page.locator('.metric-grid')
  const grid = page.locator('.metric-grid-layout')
  const cards = grid.locator('.metric-card')
  const columns = async () => {
    const boxes = await cards.evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().x)))
    return new Set(boxes).size
  }
  await page.setViewportSize({ width: 1600, height: 900 })
  await container.evaluate((el) => { el.style.width = '760px' })
  await expect.poll(columns).toBe(4)
  await container.evaluate((el) => { el.style.width = '500px' })
  await expect.poll(columns).toBe(2)
  await container.evaluate((el) => { el.style.width = '340px' })
  await expect.poll(columns).toBe(1)
  await page.evaluate(() => document.body.style.zoom = '1.2')
  await expect(cards.nth(7)).toBeVisible()
})

test('selects each multi-file source without concatenating contents', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  await mock.frame({ type: 'project_state', hardware_project: {
    schema: 'hardware-project/v1', revision: 2, digest: 'a'.repeat(64),
    top_module: 'soc_top', compile_order: ['rtl/alu.sv', 'rtl/soc_top.sv'],
    include_dirs: ['rtl'], defines: {}, testbenches: [], constraints: [],
    build_targets: {},
    files: [
      { path: 'rtl/alu.sv', role: 'rtl', language: 'systemverilog', sha256: 'b'.repeat(64), size: 32, content: 'module alu;\nendmodule\n' },
      { path: 'rtl/soc_top.sv', role: 'rtl', language: 'systemverilog', sha256: 'c'.repeat(64), size: 36, content: 'module soc_top;\nendmodule\n' },
    ],
  } })
  await page.getByRole('button', { name: 'Sources', exact: true }).click()
  await expect(page.getByTestId('sources-view')).toBeVisible()
  await expect(page.getByTestId('source-group-rtl')).toContainText('RTL')
  await expect(page.getByText('2 files')).toBeVisible()
  await expect(page.getByText('2 compiled')).toBeVisible()
  await expect(page.getByRole('button', { name: /rtl\/alu\.sv/ })).toBeVisible()
  await page.getByRole('button', { name: /rtl\/soc_top\.sv/ }).click()
  await expect(page.getByText('module soc_top;', { exact: true })).toBeVisible()
  await expect(page.getByText('module alu;', { exact: true })).toHaveCount(0)
})


test('filters authoritative sources and exposes selected-file metadata', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  await mock.frame({ type: 'project_state', hardware_project: {
    schema: 'hardware-project/v1', revision: 3, digest: 'd'.repeat(64), top_module: 'chip_top',
    compile_order: ['rtl/pkg.sv', 'rtl/chip_top.sv'], include_dirs: ['rtl'], defines: { SYNTHESIS: '1' },
    testbenches: ['tb/chip_tb.sv'], constraints: ['constraints/chip.sdc'], build_targets: {},
    provenance: { name: 'Pinned core', commit: '1234567890abcdef' },
    files: [
      { path: 'rtl/pkg.sv', role: 'package', language: 'systemverilog', sha256: 'a'.repeat(64), size: 24, content: 'package pkg; endpackage\n' },
      { path: 'rtl/chip_top.sv', role: 'rtl', language: 'systemverilog', sha256: 'b'.repeat(64), size: 32, content: 'module chip_top; endmodule\n' },
      { path: 'tb/chip_tb.sv', role: 'testbench', language: 'systemverilog', sha256: 'c'.repeat(64), size: 28, content: 'module chip_tb; endmodule\n' },
      { path: 'constraints/chip.sdc', role: 'constraint', language: 'sdc', sha256: 'e'.repeat(64), size: 20, content: 'create_clock clk\n' },
    ],
  } })
  await page.getByRole('button', { name: 'Sources', exact: true }).click()
  await expect(page.getByTestId('source-group-packages')).toBeVisible()
  await expect(page.getByTestId('source-group-testbenches')).toBeVisible()
  await expect(page.getByTestId('source-group-constraints')).toBeVisible()
  await page.getByLabel('Filter source paths').fill('chip.sdc')
  await expect(page.getByRole('button', { name: /constraints\/chip\.sdc/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /rtl\/chip_top\.sv/ })).toHaveCount(0)
  await page.getByRole('button', { name: /constraints\/chip\.sdc/ }).click()
  await expect(page.getByText('sha256 eeeeeeeeeeee')).toBeVisible()
  await expect(page.getByText('create_clock clk', { exact: true })).toBeVisible()
})
