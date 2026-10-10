import { test, expect } from '@playwright/test'
import { installMockBackend } from './fixtures'
import { C5_BENCHMARKS, c5Graph, c5Metadata } from './c5-netlist-fixtures.js'

const JOB_IDS = Object.fromEntries(Object.keys(C5_BENCHMARKS).map((name) => [name, `j-c5-${name}`]))

function installC5Backend(page, benchmark, { holdFirstGraph = false } = {}) {
  const jobId = JOB_IDS[benchmark]
  installMockBackend(page)
  page.route('http://127.0.0.1:8000/api/jobs', (route) => route.fulfill({ json: [{
    id: jobId, kind: 'synthesis', design: C5_BENCHMARKS[benchmark].module,
    status: 'done', outcome: 'success', session: 'proj-a', created_at: new Date(1_000).toISOString(),
  }] }))
  page.route(`http://127.0.0.1:8000/api/jobs/${jobId}/netlist`, (route) =>
    route.fulfill({ json: c5Metadata(benchmark, jobId) }))
  let firstGraph = true
  let releaseFirstGraph
  const firstGraphReleased = new Promise((resolve) => { releaseFirstGraph = resolve })
  page.route(new RegExp(`http://127\\.0\\.0\\.1:8000/api/jobs/${jobId}/netlist/graph`), async (route) => {
    if (holdFirstGraph && firstGraph) {
      firstGraph = false
      await firstGraphReleased
    }
    const url = new URL(route.request().url())
    const limit = Number(url.searchParams.get('limit') ?? 500)
    await route.fulfill({
      headers: { 'x-c5-benchmark': benchmark },
      json: c5Graph({ benchmark, limit }),
    })
  })
  page.route(new RegExp(`http://127\\.0\\.0\\.1:8000/api/jobs/${jobId}/timing-paths`), (route) =>
    route.fulfill({ json: { schema: 'timing-paths/v1', available: false, reason: 'C5 fixture has no timing-path report' } }))
  return { releaseFirstGraph }
}

async function openC5Visualize(page, benchmark) {
  await page.goto('/')
  await page.getByText('Alpha', { exact: true }).first().click()
  await page.getByText('Visualize', { exact: true }).click()
  await expect(page.getByTestId('gate-schematic')).toBeVisible()
  await expect(page.getByTestId('gate-schematic-loading')).toHaveCount(0)
  await expect(page.getByTestId('gate-schematic-scope')).toContainText(C5_BENCHMARKS[benchmark].module)
}

test('C5 fixtures are bounded and deterministic across benchmark classes', async () => {
  for (const [name, spec] of Object.entries(C5_BENCHMARKS)) {
    const graph = c5Graph({ benchmark: name, limit: 500 })
    expect(graph.counts.nodes).toBeLessThanOrEqual(500)
    expect(graph.counts.cells).toBe(Math.min(spec.cells, 498))
    expect(JSON.stringify(graph).length).toBeLessThan(500_000)
    expect(graph.completeness.cell_count).toBe(spec.cells)
    expect(graph.truncated).toBe(spec.cells > 498)
  }
})

test('medium benchmark supports search, selection, zoom, and fit within interaction budget', async ({ page }) => {
  installC5Backend(page, 'medium')
  const started = Date.now()
  await openC5Visualize(page, 'medium')
  const loadMs = Date.now() - started
  test.info().annotations.push({ type: 'c5-load-ms', description: String(loadMs) })

  const search = page.getByLabel('Search instances, cells, signals, sources')
  await search.fill('u00137')
  await expect(page.getByTestId('gate-schematic-search-results')).toContainText('u00137')
  await page.getByTestId('gate-schematic-search-results').getByRole('button').first().click()
  await expect(page.getByTestId('gate-schematic-details')).toContainText('u00137')

  const scale = page.locator('[data-testid="gate-schematic"] .text-gray-600').filter({ hasText: /%/ }).first()
  const readScale = async () => Number((await scale.textContent()).replace('%', ''))
  const initialScale = await readScale()
  await page.getByLabel('Zoom in').click()
  await expect.poll(readScale).toBeGreaterThan(initialScale)
  const zoomedScale = await readScale()
  await page.getByLabel('Zoom out').click()
  await expect.poll(readScale).toBe(initialScale)
  expect(zoomedScale).toBeGreaterThan(initialScale)
  await page.getByLabel('Fit to view').click()
  await expect(page.getByTestId('gate-schematic')).toBeVisible()
})

test('@perf large benchmark keeps transfer and rendering bounded, with explicit truncation and limit escalation', async ({ page }) => {
  installC5Backend(page, 'large')
  await page.addInitScript(() => {
    window.__c5LongTasks = []
    if (typeof PerformanceObserver !== 'undefined') {
      try {
        const observer = new PerformanceObserver((list) => {
          window.__c5LongTasks.push(...list.getEntries().map((entry) => ({
            startTime: entry.startTime,
            duration: entry.duration,
          })))
        })
        observer.observe({ type: 'longtask', buffered: true })
      } catch { /* Chromium builds without longtask support use the RAF metric. */ }
    }
  })
  const cdp = await page.context().newCDPSession(page)
  const heap = async (label) => {
    await cdp.send('HeapProfiler.collectGarbage')
    const usage = await cdp.send('Runtime.getHeapUsage')
    test.info().annotations.push({ type: 'c5-js-heap-bytes', description: `${label} ${usage.usedSize}` })
    return usage
  }
  const graphResponses = []
  page.on('response', async (response) => {
    if (!response.url().includes('/netlist/graph')) return
    const body = await response.body()
    graphResponses.push({ url: response.url(), bytes: body.length })
  })
  const started = Date.now()
  await page.goto('/')
  await page.getByText('Alpha', { exact: true }).first().click()
  const heapBaseline = await heap('baseline-before-visualization')
  await page.getByText('Visualize', { exact: true }).click()
  await expect(page.getByTestId('gate-schematic')).toBeVisible()
  await expect(page.getByTestId('gate-schematic-loading')).toHaveCount(0)
  await expect(page.getByTestId('gate-schematic-scope')).toContainText(C5_BENCHMARKS.large.module)
  const firstLoadMs = Date.now() - started
  test.info().annotations.push({ type: 'c5-large-load-ms', description: String(firstLoadMs) })
  expect(firstLoadMs).toBeLessThan(4_000)
  await expect(page.getByTestId('gate-schematic-truncated')).toContainText('150000')
  await expect(page.locator('[data-testid="gate-schematic"] svg[role="img"] [role="button"]')).toHaveCount(500)
  const heap500 = await heap('bounded-500-nodes')

  const raise = page.getByTestId('gate-schematic-truncated').getByRole('button')
  const nextResponse = page.waitForResponse((response) => response.url().includes('/netlist/graph') && response.url().includes('limit=1000'))
  await raise.click()
  await nextResponse
  await expect(page.getByTestId('gate-schematic-truncated')).toBeVisible()
  await expect(page.locator('[data-testid="gate-schematic"] svg[role="img"] [role="button"]')).toHaveCount(1000)
  const heap1000 = await heap('bounded-1000-nodes')

  // Measure the synchronous viewport interaction through the next animation
  // frame. This remains useful on Chromium versions without longtask entries.
  const zoomLatencyMs = await page.evaluate(() => new Promise((resolve) => {
    const button = document.querySelector('[data-testid="gate-schematic"] button[aria-label="Zoom in"]')
    const startedAt = performance.now()
    button?.click()
    requestAnimationFrame(() => resolve(performance.now() - startedAt))
  }))
  test.info().annotations.push({ type: 'c5-zoom-raf-ms', description: String(Math.round(zoomLatencyMs)) })
  expect(zoomLatencyMs).toBeLessThan(250)
  await page.getByLabel('Zoom out').click()

  await expect.poll(() => graphResponses.length, { timeout: 2_000 }).toBeGreaterThanOrEqual(2)
  for (const item of graphResponses) {
    test.info().annotations.push({ type: 'c5-graph-bytes', description: `${item.url} ${item.bytes}` })
  }
  const memory = await page.evaluate(async () => {
    if (typeof performance.measureUserAgentSpecificMemory !== 'function') return null
    try { return await performance.measureUserAgentSpecificMemory() } catch { return null }
  })
  if (memory?.bytes) {
    test.info().annotations.push({ type: 'c5-browser-memory-bytes', description: String(memory.bytes) })
  }
  const longTasks = await page.evaluate(() => window.__c5LongTasks ?? [])
  const maxLongTask = Math.max(0, ...longTasks.map((entry) => entry.duration))
  test.info().annotations.push({ type: 'c5-longtask-count', description: String(longTasks.length) })
  if (longTasks.length > 0) test.info().annotations.push({ type: 'c5-max-longtask-ms', description: String(Math.round(maxLongTask)) })
  const heapGrowth = heap1000.usedSize - heapBaseline.usedSize
  test.info().annotations.push({ type: 'c5-js-heap-growth-bytes', description: String(heapGrowth) })
  expect(heap500.usedSize - heapBaseline.usedSize).toBeLessThan(128 * 1024 * 1024)
  expect(heapGrowth).toBeLessThan(128 * 1024 * 1024)
  await page.goto('/')
  await expect(page.getByText('Alpha', { exact: true }).first()).toBeVisible()
  const heapAfterCleanup = await heap('after-navigation-cleanup')
  const residual = heapAfterCleanup.usedSize - heapBaseline.usedSize
  test.info().annotations.push({ type: 'c5-js-heap-residual-bytes', description: String(residual) })
  // This is JS heap only. Browser RSS, GPU memory, and worker process memory
  // are outside Runtime.getHeapUsage and require a separate OS-level probe.
  expect(residual).toBeLessThan(64 * 1024 * 1024)
  expect(graphResponses.every((item) => item.bytes < 2_000_000), JSON.stringify(graphResponses)).toBe(true)
  expect(graphResponses.every((item) => /limit=(500|1000)/.test(item.url))).toBe(true)
})

test('large benchmark preserves bounded truncation and limit escalation', async ({ page }) => {
  installC5Backend(page, 'large')
  await openC5Visualize(page, 'large')
  await expect(page.getByTestId('gate-schematic-truncated')).toContainText('150000')
  await expect(page.locator('[data-testid="gate-schematic"] svg[role="img"] [role="button"]')).toHaveCount(500)

  const nextResponse = page.waitForResponse((response) => response.url().includes('/netlist/graph') && response.url().includes('limit=1000'))
  await page.getByTestId('gate-schematic-truncated').getByRole('button').click()
  await nextResponse
  await expect(page.locator('[data-testid="gate-schematic"] svg[role="img"] [role="button"]')).toHaveCount(1000)
  await expect(page.getByTestId('gate-schematic-truncated')).toContainText('node limit reached')
})

test('superseded large graph request is ignored after navigation', async ({ page }) => {
  const backend = installC5Backend(page, 'large', { holdFirstGraph: true })
  await page.goto('/')
  await page.getByText('Alpha', { exact: true }).first().click()
  await page.getByText('Visualize', { exact: true }).click()
  await expect(page.getByTestId('gate-schematic-loading')).toBeVisible()

  // The explicit release occurs after the page has left the visualization;
  // the late 500-node response must never repaint a stale job/scope.
  await page.getByRole('button', { name: '← Projects' }).click()
  backend.releaseFirstGraph()
  await expect(page.getByText('Alpha', { exact: true }).first()).toBeVisible()
  await expect(page.getByTestId('gate-schematic')).toHaveCount(0)
})
