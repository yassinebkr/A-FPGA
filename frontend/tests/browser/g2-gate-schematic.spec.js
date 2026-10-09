import { test, expect } from '@playwright/test'
import { installMockBackend } from './fixtures'
import { counterGraph, installNetlistMock } from './g2-gate-schematic-mock.js'

async function openVisualize(page, mock) {
  await page.goto('/')
  await page.getByText('Alpha', { exact: true }).first().click()
  // The cross-probing test needs a source manifest: schematic source
  // attributes resolve against these project files.
  await mock.frame({ type: 'project_state', hardware_project: {
    schema: 'hardware-project/v1', revision: 1, digest: 'e'.repeat(64), top_module: 'counter8',
    compile_order: ['rtl/counter8.v', 'rtl/alu_slice.v'], include_dirs: ['rtl'], defines: {},
    testbenches: [], constraints: [], build_targets: {},
    files: [
      { path: 'rtl/counter8.v', role: 'rtl', language: 'verilog', sha256: 'f'.repeat(64), size: 40, content: 'module counter8;\nendmodule\n' },
      { path: 'rtl/alu_slice.v', role: 'rtl', language: 'verilog', sha256: '0'.repeat(64), size: 40, content: 'module alu_slice;\nendmodule\n' },
    ],
  } })
  await page.getByText('Visualize', { exact: true }).click()
  await expect(page.getByTestId('gate-schematic')).toBeVisible()
}


test('renders the counter fixture: ports, sequential cell, logic, connections, minimap', async ({ page }) => {
  const mock = installMockBackend(page)
  page.route('http://127.0.0.1:8000/api/jobs', (route) =>
    route.fulfill({ json: [{ id: 'j-a-syn', kind: 'synthesis', design: 'counter8', status: 'done', outcome: 'success', session: 'proj-a', created_at: new Date(1_000).toISOString() }] }))
  installNetlistMock(page)
  await openVisualize(page, mock)

  await expect(page.getByTestId('gate-schematic-scope')).toContainText('counter8')
  const svg = page.locator('[data-testid="gate-schematic"] svg[role="img"]')
  await expect(svg).toBeVisible()
  await expect(svg).toContainText('clk')
  await expect(svg).toContainText('_ff0_')
  await expect(svg).toContainText('dfxtp_1')
  await expect(svg).toContainText('_slice_')
  // Directed connections rendered as wires.
  expect(await svg.locator('polyline').count()).toBeGreaterThanOrEqual(2)
  await expect(page.getByTestId('gate-schematic-minimap')).toBeVisible()
  // Identity strip is digest-bound.
  await expect(page.locator('[data-testid="gate-schematic"]')).toContainText('sky130_fd_sc_hd')
  await expect(page.locator('[data-testid="gate-schematic"]')).toContainText('artifact aaaaaaaaaa')
})

test('selects a node, expands hierarchy, and cross-probes back and forth with Sources', async ({ page }) => {
  const mock = installMockBackend(page)
  page.route('http://127.0.0.1:8000/api/jobs', (route) =>
    route.fulfill({ json: [{ id: 'j-a-syn', kind: 'synthesis', design: 'counter8', status: 'done', outcome: 'success', session: 'proj-a', created_at: new Date(1_000).toISOString() }] }))
  installNetlistMock(page)
  await openVisualize(page, mock)

  // Select the hierarchical instance.
  await page.locator('[aria-label*="_slice_"]').click()
  await expect(page.getByTestId('gate-schematic-details')).toContainText('alu_slice')
  await expect(page.getByTestId('gate-schematic-details')).toContainText('rtl/counter8.v:15')

  // Cross-probe into the Sources surface: the referenced file is selected.
  await page.getByTestId('gate-schematic-details').getByText('rtl/counter8.v:15').click()
  await expect(page.getByTestId('sources-view')).toBeVisible()
  await expect(page.locator('[data-testid="source-group-rtl"] button.bg-emerald-500\\/5, [data-testid="source-group-rtl"] .bg-emerald-500\\/5')).toBeVisible()

  // Return to Visualize: the component stayed mounted, so the same selection
  // is still inspected.
  await page.getByText('Visualize', { exact: true }).click()
  await expect(page.getByTestId('gate-schematic-details')).toContainText('_slice_')

  // Expand the hierarchy into the child module scope.
  await page.locator('[aria-label*="_slice_"]').click()
  await page.getByTestId('gate-schematic-expand').click()
  await expect(page.getByTestId('gate-schematic-scope')).toContainText('alu_slice')
  await expect(page.locator('[data-testid="gate-schematic"] svg[role="img"]')).toContainText('_xor_')
  // Back returns to the top scope.
  await page.getByLabel('Back to parent scope').click()
  await expect(page.getByTestId('gate-schematic-scope')).toContainText('counter8')
})

test('selection survives tab switches; jobs without an artifact say so honestly', async ({ page }) => {
  const mock = installMockBackend(page)
  page.route('http://127.0.0.1:8000/api/jobs', (route) =>
    route.fulfill({ json: [
      { id: 'j-a-syn', kind: 'synthesis', design: 'counter8', status: 'done', outcome: 'success', session: 'proj-a', created_at: new Date(2_000).toISOString() },
      { id: 'j-z-none', kind: 'synthesis', design: 'other', status: 'done', outcome: 'success', session: 'proj-a', created_at: new Date(1_000).toISOString() },
    ] }))
  installNetlistMock(page)
  page.route('http://127.0.0.1:8000/api/jobs/j-z-none/netlist', (route) =>
    route.fulfill({ json: { schema: 'gate-netlist/v1', available: false, reason: 'no structured synthesis output was produced by this job' } }))
  await openVisualize(page, mock)

  await page.locator('[aria-label*="_ff0_"]').click()
  await expect(page.getByTestId('gate-schematic-details')).toContainText('_ff0_')
  // Switch away and back: the same node stays selected (Phase G2 acceptance).
  await page.getByRole('button', { name: 'Sources', exact: true }).click()
  await page.getByRole('button', { name: 'Visualize', exact: true }).click()
  await expect(page.getByTestId('gate-schematic-details')).toContainText('_ff0_')

  // Select the job without an artifact: explicit unavailable, no diagram.
  await page.getByRole('button', { name: /^Jobs/ }).click()
  await page.getByText('j-z-none').click()
  await page.getByText('Visualize', { exact: true }).click()
  await expect(page.getByTestId('gate-schematic-unavailable')).toBeVisible()
  await expect(page.getByTestId('gate-schematic-reason')).toContainText('no structured synthesis output')
})

test('shows the truncated state with a bounded view and a raise-limit action', async ({ page }) => {
  const mock = installMockBackend(page)
  page.route('http://127.0.0.1:8000/api/jobs', (route) =>
    route.fulfill({ json: [{ id: 'j-a-syn', kind: 'synthesis', design: 'counter8', status: 'done', outcome: 'success', session: 'proj-a', created_at: new Date(1_000).toISOString() }] }))
  installNetlistMock(page, {
    graphHandler: (url) => {
      const limit = Number(url.searchParams.get('limit') ?? 500)
      // A large design stays truncated until the user explicitly raises the
      // bounded node limit.
      return counterGraph({ truncated: limit < 1000 })
    },
  })
  await openVisualize(page, mock)
  const banner = page.getByTestId('gate-schematic-truncated')
  await expect(banner).toBeVisible()
  await expect(banner).toContainText('view truncated')
  await banner.getByRole('button').click()
  await expect(banner).toBeHidden()
})

test('keyboard navigation moves the selection and reduced motion is respected', async ({ page }) => {
  const mock = installMockBackend(page)
  page.route('http://127.0.0.1:8000/api/jobs', (route) =>
    route.fulfill({ json: [{ id: 'j-a-syn', kind: 'synthesis', design: 'counter8', status: 'done', outcome: 'success', session: 'proj-a', created_at: new Date(1_000).toISOString() }] }))
  installNetlistMock(page)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await openVisualize(page, mock)

  await page.locator('[aria-label="port clk"]').click()
  await expect(page.getByTestId('gate-schematic-details')).toContainText('clk')
  // Arrow selection moves to the nearest node to the right: the layer-1
  // cell on the same row (deterministic layered layout).
  await page.locator('[aria-label="Gate schematic canvas: arrow keys move the selection, plus and minus zoom, f fits"]').focus()
  await page.keyboard.press('ArrowRight')
  await expect(page.getByTestId('gate-schematic-details')).toContainText('_and0_')
  await page.keyboard.press('ArrowDown')
  await expect(page.getByTestId('gate-schematic-details')).toContainText('_ff0_')
  await page.keyboard.press('f') // fit still works under reduced motion
  await expect(page.getByTestId('gate-schematic')).toBeVisible()
})

test('fullscreen mode expands the visualization overlay and Escape exits it', async ({ page }) => {
  const mock = installMockBackend(page)
  page.route('http://127.0.0.1:8000/api/jobs', (route) =>
    route.fulfill({ json: [{ id: 'j-a-syn', kind: 'synthesis', design: 'counter8', status: 'done', outcome: 'success', session: 'proj-a', created_at: new Date(1_000).toISOString() }] }))
  installNetlistMock(page)
  await openVisualize(page, mock)
  await expect(page.getByTestId('gate-schematic-scope')).toContainText('counter8')

  // The toggle expands the panel to a fullscreen overlay while keeping the
  // same React tree (scope and selection survive, no remount).
  await page.getByTestId('visualize-fullscreen-toggle').click()
  const overlay = page.getByTestId('visualization-foundation')
  await expect(overlay).toHaveClass(/fixed/)
  await expect(overlay).toContainText('counter8')

  // Escape exits the overlay.
  await page.keyboard.press('Escape')
  await expect(overlay).not.toHaveClass(/fixed/)
  await expect(page.getByTestId('gate-schematic-scope')).toContainText('counter8')
})
