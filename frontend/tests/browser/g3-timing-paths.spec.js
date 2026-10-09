import { test, expect } from '@playwright/test'
import { installMockBackend } from './fixtures'
import { ARTIFACT_SHA, NETLIST_SHA, installNetlistMock } from './g2-gate-schematic-mock.js'

// Phase G3 browser coverage: choosing a timing path opens its cone,
// highlights the ordered path, dims unrelated logic, shows the delay
// breakdown, and rejects paths from a different netlist.

function timingPathsDoc(netlistSha, { partialHold = true } = {}) {
  const setup = {
    analysis: 'setup', startpoint: '_ff0_', endpoint: 'q[0]',
    clock: 'clk', corner: 'slow', path_group: 'clk',
    slack_ns: 6.0, data_arrival_ns: 1.75, data_required_ns: 7.75, met: true,
    segments: [
      { kind: 'clock', name: 'clock clk (rise edge)', cell_type: null, delay_ns: 0, time_ns: 0, description: 'clock clk (rise edge)' },
      { kind: 'cell_pin', name: '_ff0_/CLK', cell_type: 'sky130_fd_sc_hd__dfxtp_1', delay_ns: 0, time_ns: 0, description: '_ff0_/CLK', graph_id: 'cell:_ff0_', pin: 'CLK', module: 'counter8' },
      { kind: 'cell_pin', name: '_ff0_/Q', cell_type: 'sky130_fd_sc_hd__dfxtp_1', delay_ns: 1.75, time_ns: 1.75, description: '_ff0_/Q', graph_id: 'cell:_ff0_', pin: 'Q', module: 'counter8' },
      { kind: 'net', name: 'q[0]', cell_type: null, delay_ns: null, time_ns: null, description: 'q[0] (net)', graph_id: 'net:counter8:n2', module: 'counter8' },
      { kind: 'port', name: 'q[0]', cell_type: null, delay_ns: 0, time_ns: 1.75, description: 'q[0] (out)', graph_id: 'port:q', module: 'counter8' },
    ],
    mapping: { resolved: 4, unresolved: [], partial: false },
  }
  const hold = {
    analysis: 'hold', startpoint: '_and0_', endpoint: '_ff0_',
    clock: 'clk', corner: 'fast', path_group: 'clk',
    slack_ns: -0.13, data_arrival_ns: 0.43, data_required_ns: 0.3, met: false,
    segments: [
      { kind: 'cell_pin', name: '_and0_/A', cell_type: 'sky130_fd_sc_hd__and2_0', delay_ns: 0, time_ns: 0, description: '_and0_/A', graph_id: 'cell:_and0_', pin: 'A', module: 'counter8' },
      { kind: 'cell_pin', name: '_and0_/X', cell_type: 'sky130_fd_sc_hd__and2_0', delay_ns: 0.31, time_ns: 0.31, description: '_and0_/X', graph_id: 'cell:_and0_', pin: 'X', module: 'counter8' },
      ...(partialHold ? [{
        kind: 'cell_pin', name: '_missing_/A', cell_type: 'sky130_fd_sc_hd__nand2_1',
        delay_ns: 0, time_ns: 0.31, description: '_missing_/A', graph_id: null,
      }] : []),
      { kind: 'cell_pin', name: '_ff0_/D', cell_type: 'sky130_fd_sc_hd__dfxtp_1', delay_ns: 0.12, time_ns: 0.43, description: '_ff0_/D', graph_id: 'cell:_ff0_', pin: 'D', module: 'counter8' },
    ],
    mapping: partialHold
      ? { resolved: 3, unresolved: ['_missing_/A'], partial: true }
      : { resolved: 4, unresolved: [], partial: false },
  }
  return {
    schema: 'timing-paths/v1',
    identity: {
      job_id: 'j-a-syn', netlist_sha256: netlistSha,
      gate_artifact_sha256: ARTIFACT_SHA, tool: 'opensta',
      top_module: 'counter8', sources: ['setup.topN', 'hold.fast.topN.fast'],
      path_count: 2, complete: !partialHold,
    },
    paths: [setup, hold],
  }
}

function installTimingPathsMock(page, { netlistSha = NETLIST_SHA } = {}) {
  page.route(/\/api\/jobs\/[^/]+\/timing-paths/, (route) => {
    const url = new URL(route.request().url())
    const requested = url.searchParams.get('netlist_sha256')
    if (requested && requested !== netlistSha) {
      return route.fulfill({ status: 409, json: { detail: 'netlist digest mismatch: these timing paths belong to a different netlist' } })
    }
    return route.fulfill({ json: timingPathsDoc(netlistSha) })
  })
}

async function openVisualize(page) {
  await page.goto('/')
  await page.getByText('Alpha', { exact: true }).first().click()
  await page.getByText('Visualize', { exact: true }).click()
  await expect(page.getByTestId('gate-schematic')).toBeVisible()
}

function installSynthesisJob(page) {
  page.route('http://127.0.0.1:8000/api/jobs', (route) =>
    route.fulfill({ json: [{ id: 'j-a-syn', kind: 'synthesis', design: 'counter8', status: 'done', outcome: 'success', session: 'proj-a', created_at: new Date(1_000).toISOString() }] }))
}

test('selecting a timing path opens its cone, highlights it, and dims unrelated logic', async ({ page }) => {
  installMockBackend(page)
  installSynthesisJob(page)
  installNetlistMock(page)
  installTimingPathsMock(page)
  await openVisualize(page)

  // Setup and hold paths, corners, and clocks are all visible and distinct.
  const strip = page.getByTestId('gate-schematic-timing')
  await expect(strip).toContainText('setup')
  await expect(strip).toContainText('slow')
  await expect(strip).toContainText('hold')
  await expect(strip).toContainText('fast')
  await expect(strip).toContainText('clk')

  // Choose the setup path: the cone opens on its first mapped cell.
  await page.getByTestId('gate-schematic-path-0').click()
  await expect(page.getByTestId('gate-schematic-cone')).toContainText('cell:_ff0_')

  // The ordered path is highlighted (amber ring on its nodes) and unrelated
  // logic is dimmed.
  const amberNodes = await page.locator('[data-testid="gate-schematic"] svg rect[stroke="#fbbf24"]').count()
  expect(amberNodes).toBeGreaterThan(0)
  const dimmed = await page.evaluate(() => {
    const nodes = [...document.querySelectorAll('svg[role="img"] g[opacity="0.25"]')]
    return nodes.map((node) => node.getAttribute('aria-label'))
  })
  expect(dimmed).toContain('cell _slice_, type alu_slice, hierarchy alu_slice')

  // Compact delay breakdown: analysis, corner, clock, slack, ordered
  // segments with delay and cumulative time.
  const detail = page.getByTestId('gate-schematic-path-detail')
  await expect(detail).toBeVisible()
  await expect(detail.getByTestId('gate-schematic-path-analysis')).toHaveText('setup')
  await expect(detail).toContainText('corner')
  await expect(detail).toContainText('slow')
  await expect(detail).toContainText('6 ns slack')
  await expect(detail).toContainText('_ff0_/Q')
  await expect(detail).toContainText('q[0]')

  // Cross-probe from the timing row into the schematic: the segment click
  // selects the mapped node.
  await detail.getByText('_ff0_/Q').click()
  await expect(page.getByTestId('gate-schematic-details')).toContainText('_ff0_')

  // Clearing the path restores the full view.
  await page.getByTestId('gate-schematic-clear-path').click()
  await expect(page.getByTestId('gate-schematic-path-detail')).toBeHidden()
  await expect(page.getByTestId('gate-schematic-cone')).toBeHidden()
})

test('partial mapping is labeled and lists every unresolved segment', async ({ page }) => {
  installMockBackend(page)
  installSynthesisJob(page)
  installNetlistMock(page)
  installTimingPathsMock(page)
  await openVisualize(page)

  // The hold path carries an unresolved segment; the strip marks it partial.
  await expect(page.getByTestId('gate-schematic-path-1')).toContainText('partial')
  await page.getByTestId('gate-schematic-path-1').click()
  const detail = page.getByTestId('gate-schematic-path-detail')
  await expect(detail).toContainText('partial mapping — 1 unresolved segment(s)')
  await expect(detail).toContainText('_missing_/A')
})

test('a path from a different netlist is rejected, not silently highlighted', async ({ page }) => {
  installMockBackend(page)
  installSynthesisJob(page)
  installNetlistMock(page)
  // The artifact serves netlist NETLIST_SHA; the timing paths claim another.
  installTimingPathsMock(page, { netlistSha: 'f'.repeat(64) })
  await openVisualize(page)

  await page.getByTestId('gate-schematic-path-0').click()
  await expect(page.getByTestId('gate-schematic-path-mismatch')).toContainText('different netlist')
  await expect(page.getByTestId('gate-schematic-path-detail')).toBeHidden()
  await expect(page.getByTestId('gate-schematic-cone')).toBeHidden()
})

test('a job without ordered STA reports shows the honest unavailable state', async ({ page }) => {
  installMockBackend(page)
  installSynthesisJob(page)
  installNetlistMock(page)
  page.route(/\/api\/jobs\/[^/]+\/timing-paths/, (route) =>
    route.fulfill({ json: { schema: 'timing-paths/v1', available: false, reason: 'the STA run produced no ordered timing-path reports' } }))
  await openVisualize(page)
  await expect(page.getByTestId('gate-schematic-timing-unavailable')).toContainText('no ordered timing-path reports')
  await expect(page.getByTestId('gate-schematic-timing')).toBeHidden()
})
