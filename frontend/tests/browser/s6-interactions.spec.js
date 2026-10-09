import { test, expect } from '@playwright/test'
import { installMockBackend } from './fixtures'

async function openProject(page, name = 'Alpha') {
  await page.goto('/')
  await page.getByText(name, { exact: true }).first().click()
  await expect(page.getByText('Synthesis Workbench')).toBeVisible()
}

async function reasoningTrigger(page) {
  return page.getByRole('button', { name: /model reasoning/i })
}

async function reasoningPanel(page) {
  const trigger = await reasoningTrigger(page)
  return trigger.locator('xpath=..').getByRole('region', { name: 'Full model reasoning' })
}

test.describe('S6 reasoning and status interactions', () => {
  test('keeps reasoning folded, supports hover/pin, outside dismissal, trigger toggle, and Escape', async ({ page }) => {
    const mock = installMockBackend(page)
    await openProject(page)
    await mock.reasoningStart()
    await mock.reasoning('first complete reasoning line')

    const trigger = await reasoningTrigger(page)
    await expect(trigger).toHaveAttribute('aria-expanded', 'false')
    await expect(page.getByText('first complete reasoning line')).toHaveCount(0)

    await trigger.hover()
    await expect(await reasoningPanel(page)).toBeVisible()
    await trigger.click()
    await expect(trigger).toHaveAttribute('aria-expanded', 'true')
    await expect((await reasoningPanel(page)).locator('pre')).toContainText('first complete reasoning line')

    // A second trigger click must close the pinned panel even while the
    // pointer remains over the trigger (hover must not immediately reopen it).
    await trigger.click()
    await expect(trigger).toHaveAttribute('aria-expanded', 'false')

    await trigger.click()
    await expect(trigger).toHaveAttribute('aria-expanded', 'true')
    await page.locator('h2', { hasText: 'Synthesis Workbench' }).click()
    await expect(trigger).toHaveAttribute('aria-expanded', 'false')

    await trigger.click()
    await page.keyboard.press('Escape')
    await expect(trigger).toHaveAttribute('aria-expanded', 'false')
  })

  test('hovered reasoning panel stays fully inside the viewport when the tile sits at the bottom', async ({ page }) => {
    const mock = installMockBackend(page)
    await openProject(page)
    await mock.reasoningStart()
    await mock.reasoning('bottom-anchored reasoning line')

    const trigger = await reasoningTrigger(page)
    // The tile sits at the bottom of the transcript, right above the composer:
    // a downward-anchored panel would open off-screen with only its header
    // sliver visible. The panel must anchor toward the roomy side and stay
    // fully inside the viewport, with the reasoning text readable.
    await trigger.hover()
    const panel = await reasoningPanel(page)
    await expect(panel).toBeVisible()
    const box = await panel.boundingBox()
    expect(box.y).toBeGreaterThanOrEqual(0)
    expect(box.y + box.height).toBeLessThanOrEqual(page.viewportSize().height)
    await expect(panel).toContainText('bottom-anchored reasoning line')
  })

  test('composer interaction dismisses a pinned reasoning panel', async ({ page }) => {
    const mock = installMockBackend(page)
    await openProject(page)
    await mock.reasoningStart()
    await mock.reasoning('reasoning that should remain inspectable')

    const trigger = await reasoningTrigger(page)
    await trigger.click()
    await expect((await reasoningPanel(page)).locator('pre')).toContainText('reasoning that should remain inspectable')

    const composer = page.getByPlaceholder('Describe the hardware to build…')
    await composer.click()
    await expect(trigger).toHaveAttribute('aria-expanded', 'false')
  })

  test('chat and reasoning streams follow the bottom, then pause and resume independently', async ({ page }) => {
    const mock = installMockBackend(page)
    await openProject(page)
    await mock.reasoningStart()
    await mock.reasoning(Array.from({ length: 80 }, (_, i) => `reasoning line ${i}`).join('\n'))
    await mock.text(Array.from({ length: 80 }, (_, i) => `assistant line ${i}`).join('\n'))

    const chat = page.locator('div.flex-grow.p-4.overflow-y-auto').first()
    await expect.poll(() => chat.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true)
    await expect.poll(() => chat.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(32)

    await chat.evaluate((el) => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')) })
    const pausedTop = await chat.evaluate((el) => el.scrollTop)
    await mock.text('\nnew assistant content while paused')
    await expect.poll(() => chat.evaluate((el) => el.scrollTop)).toBe(pausedTop)

    await chat.evaluate((el) => { el.scrollTop = el.scrollHeight; el.dispatchEvent(new Event('scroll')) })
    await mock.text('\nassistant content after resume')
    await expect.poll(() => chat.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(32)

    const trigger = await reasoningTrigger(page)
    await trigger.click()
    const panel = await reasoningPanel(page)
    const reasoningLog = panel.locator('pre')
    await expect(reasoningLog).toBeVisible()
    await expect.poll(() => reasoningLog.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true)
    await reasoningLog.evaluate((el) => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')) })
    await mock.reasoning('\nreasoning content while paused')
    await expect.poll(() => reasoningLog.evaluate((el) => el.scrollTop)).toBe(0)
  })

  test('generation completion does not clear an active run status', async ({ page }) => {
    const mock = installMockBackend(page)
    await openProject(page)
    await mock.reasoningStart({ generationId: 'gen-active', round: 2 })
    await mock.reasoning('completed generation, controller still running')
    await mock.reasoningEnd({ generationId: 'gen-active', round: 2 })
    await mock.frame({
      type: 'progress',
      content: { id: 'phase-1', phase: 'simulation', action: 'Running checks', status: 'running', attempt_number: 1, attempt_limit: 3 },
    })

    const statusTrigger = page.getByRole('button', { name: 'Show run status' })
    await statusTrigger.click()
    const statusPanel = page.locator('#agent-run-status')
    await expect(statusPanel).toContainText('RUNNING')
    await expect(statusPanel).toContainText('simulation')
    await expect(statusPanel).toContainText('attempt 1/3')
    await expect(page.getByText('Reasoning complete')).toBeVisible()
  })

  test('restores an interrupted durable run state after a backend restart', async ({ page }) => {
    const mock = installMockBackend(page)
    await openProject(page)
    await mock.frame({
      type: 'project_state',
      run_state: {
        version: 1, run_id: 'run-restart', seq: 4, lifecycle: 'interrupted',
        phase: 'simulation', terminal_reason: 'Backend restarted while the run was active.',
        budgets: { tokens: { used: 1200, limit: 60000 }, rounds: { used: 2, limit: 14 } },
      },
    })
    // A later transport terminator must not erase the durable lifecycle/reason.
    await mock.frame({ type: 'end' })

    await page.getByRole('button', { name: 'Show run status' }).click()
    const panel = page.locator('#agent-run-status')
    await expect(panel).toContainText('INTERRUPTED')
    await expect(panel.getByTestId('run-status-summary')).toHaveText('Backend restarted while the run was active.')
    await expect(panel).toContainText('1.2k/60k')
    await expect(page.getByText('agent working…')).toHaveCount(0)
    await expect(page.getByPlaceholder('Describe the hardware to build…')).toBeEnabled()
  })

  test('restores a nonterminal durable run as active on reconnect', async ({ page }) => {
    const mock = installMockBackend(page)
    await openProject(page)
    await mock.frame({
      type: 'project_state',
      run_state: { version: 1, run_id: 'run-live', seq: 8, lifecycle: 'running', phase: 'synthesis', budgets: {} },
    })

    await page.getByRole('button', { name: 'Show run status' }).click()
    await expect(page.locator('#agent-run-status')).toContainText('RUNNING')
    await expect(page.getByText('agent working…')).toBeVisible()
    await expect(page.getByTitle('Stop run')).toBeVisible()
  })

  test('reasoning panel expands downward and stays inside the viewport at 120% zoom', async ({ page }) => {
    const mock = installMockBackend(page)
    await openProject(page)
    await mock.reasoningStart()
    await mock.reasoning('line\n'.repeat(120))
    await page.evaluate(() => { document.body.style.zoom = '1.2' })

    const trigger = await reasoningTrigger(page)
    await trigger.click()
    const panel = await reasoningPanel(page)
    await expect(panel).toBeVisible()
    const triggerBox = await trigger.boundingBox()
    const panelBox = await panel.boundingBox()
    const viewport = page.viewportSize()
    expect(panelBox.y).toBeGreaterThanOrEqual(triggerBox.y + triggerBox.height - 1)
    expect(panelBox.y + panelBox.height).toBeLessThanOrEqual(viewport.height + 1)
    await expect(panel.locator('pre')).toBeVisible()
  })
})

test.describe('S6 reasoning effort selector', () => {
  test('exposes the effort level for models that declare it and PATCHes the project', async ({ page }) => {
    installMockBackend(page)
    // Override the registry with a GLM-like entry declaring effort levels;
    // the project must be on that model for the selector to appear.
    await page.route('http://127.0.0.1:8000/api/models', (route) =>
      route.fulfill({ json: [
        { key: 'mock', display_name: 'Mock', available: true, default: false, health: 'ok' },
        { key: 'mistral-glm-5.3', display_name: 'GLM 5.3 (Mistral hosted)', available: true, default: true, health: 'ok',
          reasoning_efforts: ['low', 'high', 'max'], reasoning_effort_default: 'low' },
      ] }))
    // Stateful project mock: the list read after a PATCH must reflect it.
    let currentEffort = null
    const projectList = () => ([
      { id: 'proj-a', name: 'Alpha', preview: '', messages: 0, created: 2, updated: 20,
        model: 'mistral-glm-5.3', reasoning_effort: currentEffort },
    ])
    await page.route('http://127.0.0.1:8000/api/projects', (route) =>
      route.fulfill({ json: projectList() }))
    const patches = []
    await page.route('http://127.0.0.1:8000/api/projects/proj-a', async (route) => {
      if (route.request().method() === 'PATCH') {
        const body = route.request().postDataJSON()
        patches.push(body)
        currentEffort = body.reasoning_effort ?? null
        await route.fulfill({ json: { id: 'proj-a', name: 'Alpha', model: 'mistral-glm-5.3',
                                      reasoning_effort: currentEffort, updated: 21 } })
      } else {
        await route.fulfill({ json: { id: 'proj-a', name: 'Alpha', model: 'mistral-glm-5.3',
                                      reasoning_effort: currentEffort } })
      }
    })

    await page.goto('/')
    await page.getByText('Alpha', { exact: true }).first().click()
    await expect(page.getByText('Synthesis Workbench')).toBeVisible()

    const effort = page.getByRole('combobox', { name: 'Reasoning effort' })
    await expect(effort).toBeVisible()
    await expect(effort).toHaveValue('')
    await effort.selectOption('high')
    await expect.poll(() => patches.length).toBe(1)
    expect(patches[0]).toEqual({ reasoning_effort: 'high' })
    await expect(effort).toHaveValue('high')
  })
})
