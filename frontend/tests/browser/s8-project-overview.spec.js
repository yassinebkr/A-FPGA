import { test, expect } from '@playwright/test'
import { installMockBackend } from './fixtures'

async function openProject(page) {
  await page.goto('/')
  await page.getByText('Alpha', { exact: true }).first().click()
  await expect(page.getByTestId('project-overview')).toBeVisible()
}

test('shows project identity and fail-closed qualification gates in canonical order', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  await mock.frame({ type: 'project_state',
    hardware_project: {
      schema: 'hardware-project/v1', revision: 4, digest: 'a'.repeat(64), top_module: 'ibex_core',
      compile_order: ['rtl/ibex_core.sv'], include_dirs: ['rtl'], defines: {},
      testbenches: [], constraints: [], build_targets: {},
      files: [{ path: 'rtl/ibex_core.sv', role: 'rtl', language: 'systemverilog', sha256: 'b'.repeat(64), size: 40, content: 'module ibex_core; endmodule\n' }],
    },
    metrics: { setupslack: -2.32, fmax: 44.8, target_mhz: 50 },
  })
  await mock.frame({ type: 'job_started', job_id: 'syn-1', kind: 'synthesis', design: 'ibex_core', session: 'proj-a' })
  await mock.frame({ type: 'job_status', job_id: 'syn-1', status: 'done', outcome: 'success', manifest: { flow_mode: 'synthesis', metrics: { setupslack: -2.32, fmax: 44.8, target_mhz: 50 } } })

  await expect(page.getByTestId('project-top-module')).toContainText('ibex_core')
  await expect(page.getByTestId('project-source-count')).toContainText('1 sources')
  await expect(page.getByTestId('project-overview')).toContainText('goal 50 MHz')
  await expect(page.getByTestId('project-overview')).toContainText('measured 44.8 MHz')

  const keys = ['contract', 'sources', 'lint', 'simulation', 'formal', 'synthesis', 'sta', 'pnr', 'drc-lvs', 'equivalence', 'release']
  const labels = await page.getByTestId('qualification-gates').locator('[data-testid^="qualification-gate-"]').evaluateAll((nodes) => nodes.map((node) => node.dataset.testid.replace('qualification-gate-', '')))
  expect(labels).toEqual(keys)
  await expect(page.getByTestId('qualification-gate-sources')).toContainText('passed')
  await expect(page.getByTestId('qualification-gate-synthesis')).toContainText('screening')
  await expect(page.getByTestId('qualification-gate-sta')).toContainText('failed')
  await expect(page.getByTestId('qualification-gate-pnr')).toContainText('not run')
  await expect(page.getByTestId('qualification-gate-release')).toContainText('not run')
  await expect(page.getByTestId('project-overview')).toContainText('not qualified')
})

test('does not infer physical or release success from successful synthesis', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  await mock.frame({ type: 'job_started', job_id: 'syn-only', kind: 'synthesis', design: 'core', session: 'proj-a' })
  await mock.frame({ type: 'job_status', job_id: 'syn-only', status: 'done', outcome: 'success' })
  await expect(page.getByTestId('qualification-gate-synthesis')).toContainText('screening')
  await expect(page.getByTestId('qualification-gate-pnr')).not.toContainText('passed')
  await expect(page.getByTestId('qualification-gate-drc-lvs')).not.toContainText('passed')
  await expect(page.getByTestId('qualification-gate-release')).not.toContainText('passed')
})
