import { test, expect } from '@playwright/test'
import { installMockBackend } from './fixtures'
import { ARTIFACT_SHA, NETLIST_SHA, installNetlistMock } from './g2-gate-schematic-mock.js'

// Phase G4 browser coverage: the layout tab appears only for a job with a
// digest-bound layout artifact; the schematic selection locates the same
// instance in the physical view and a placed cell selects it back; a
// screening job shows the honest unavailable state.

const DEF_SHA = '1'.repeat(64)

function layoutDoc({ available = true, netlistSha = NETLIST_SHA } = {}) {
  if (!available) {
    return { schema: 'layout-view/v1', available: false,
             reason: 'the flow emitted no DEF: no physical implementation exists for this job' }
  }
  return {
    schema: 'layout-view/v1',
    identity: {
      job_id: 'j-a-syn', netlist_sha256: netlistSha,
      gate_artifact_sha256: ARTIFACT_SHA, def_sha256: DEF_SHA,
      design: 'counter8', clock_period_ns: 10,
    },
    units: 1000,
    die: { llx: 0, lly: 0, urx: 9200, ury: 6100 },
    rows: [
      { name: 'ROW_0', site: 'site', x: 0, y: 0, orientation: 'N',
        num_x: 20, num_y: 1, step_x: 460, step_y: 0 },
    ],
    components: [
      { name: '_ff0_', macro: 'sky130_fd_sc_hd__dfxtp_1', x: 460, y: 0, orientation: 'N', graph_id: 'cell:_ff0_' },
      { name: '_and0_', macro: 'sky130_fd_sc_hd__and2_0', x: 1380, y: 0, orientation: 'N', graph_id: 'cell:_and0_' },
      { name: '_unmapped_', macro: 'sky130_fd_sc_hd__buf_1', x: 1840, y: 0, orientation: 'N', graph_id: null },
    ],
    pins: [
      { name: 'clk', direction: 'INPUT', net: 'clk', x: 0, y: 1360 },
      { name: 'q', direction: 'OUTPUT', net: 'q', x: 9200, y: 1360 },
    ],
    nominal_footprint: { w: 460, h: 2720 },
    completeness: {
      component_count: 3, unplaced_count: 0, truncated: false,
      mapped_to_netlist: 2, mapping_partial: true,
      nominal_cell_footprint: true, pin_count: 2, violations_reported: false,
    },
  }
}

function installLayoutMock(page, options = {}) {
  page.route(/\/api\/jobs\/[^/]+\/layout/, (route) =>
    route.fulfill({ json: layoutDoc(options) }))
}

function installSynthesisJob(page) {
  page.route('http://127.0.0.1:8000/api/jobs', (route) =>
    route.fulfill({ json: [{ id: 'j-a-syn', kind: 'synthesis', design: 'counter8', status: 'done', outcome: 'success', session: 'proj-a', created_at: new Date(1_000).toISOString() }] }))
}

async function openVisualize(page) {
  await page.goto('/')
  await page.getByText('Alpha', { exact: true }).first().click()
  await page.getByText('Visualize', { exact: true }).click()
  await expect(page.getByTestId('gate-schematic')).toBeVisible()
}

test('layout tab renders die, rows, cells, pins and locates the schematic selection', async ({ page }) => {
  installMockBackend(page)
  installSynthesisJob(page)
  installNetlistMock(page)
  installLayoutMock(page)
  await openVisualize(page)

  // Select a cell in the schematic first (scoped: the hidden layout view
  // carries an aria-label of its own for the same instance).
  await page.getByTestId('gate-schematic').locator('[aria-label^="cell _ff0_"]').click()
  await expect(page.getByTestId('gate-schematic-details')).toContainText('_ff0_')

  // Switch to the physical view: the same instance is located and centered.
  await page.getByTestId('visualize-subview-layout').click()
  const layout = page.getByTestId('physical-layout')
  await expect(layout).toBeVisible()
  await expect(layout).toContainText('physical layout')
  await expect(layout).toContainText('counter8')
  await expect(layout).toContainText('die 9.2 x 6.1 um')
  await expect(layout).toContainText('3 placed cells')
  await expect(layout).toContainText('nominal footprints')
  await expect(layout).toContainText('partial netlist mapping')
  const selectedFill = await page.evaluate(() =>
    document.querySelector('[data-testid="physical-layout"] rect[fill="#34d399"]')?.getAttribute('aria-label'))
  expect(selectedFill).toContain('_ff0_')

  // And back to the schematic: the selection is intact (no loss between views).
  await page.getByTestId('visualize-subview-schematic').click()
  await expect(page.getByTestId('gate-schematic-details')).toContainText('_ff0_')
})

test('clicking a placed cell in the layout selects it in the schematic (bidirectional probing)', async ({ page }) => {
  installMockBackend(page)
  installSynthesisJob(page)
  installNetlistMock(page)
  installLayoutMock(page)
  await openVisualize(page)
  await page.getByTestId('visualize-subview-layout').click()
  await expect(page.getByTestId('physical-layout')).toBeVisible()

  await page.locator('[aria-label*="placed cell _and0_"]').click()
  // The layout view stays; the schematic receives the selection.
  await page.getByTestId('visualize-subview-schematic').click()
  await expect(page.getByTestId('gate-schematic-details')).toContainText('_and0_')
})

test('a screening job shows the honest layout-unavailable state', async ({ page }) => {
  installMockBackend(page)
  installSynthesisJob(page)
  installNetlistMock(page)
  installLayoutMock(page, { available: false })
  await openVisualize(page)

  await page.getByTestId('visualize-subview-layout').click()
  await expect(page.getByTestId('physical-layout-unavailable')).toBeVisible()
  await expect(page.getByTestId('physical-layout-reason')).toContainText('no physical implementation')
  // The tab itself reflects the honest availability.
  await expect(page.getByTestId('visualize-subview-layout')).toContainText('unavailable')
})
