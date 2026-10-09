import { test, expect } from '@playwright/test'
import { installMockBackend } from './fixtures'

async function openProject(page) {
  await page.goto('/')
  await page.getByText('Alpha', { exact: true }).first().click()
  await expect(page.getByTestId('project-overview')).toBeVisible()
}

async function loadState(mock, metrics) {
  await mock.frame({ type: 'project_state', metrics })
}

test('qualification chips stay in one compact band at desktop width', async ({ page }) => {
  const mock = installMockBackend(page)
  await page.setViewportSize({ width: 1600, height: 900 })
  await openProject(page)
  await loadState(mock, { setupslack: -2.32, fmax: 44.8, target_mhz: 50 })

  const strip = page.getByTestId('qualification-gates')
  await expect(strip.locator('[data-testid^="qualification-gate-"]')).toHaveCount(11)

  // One band: the chip strip and the next-action indicator vertically overlap
  // the identity block instead of occupying a second tall row.
  const boxes = await page.evaluate(() => {
    const identity = document.querySelector('[data-testid="project-top-module"]').getBoundingClientRect()
    const gates = document.querySelector('[data-testid="qualification-gates"]').getBoundingClientRect()
    const next = [...document.querySelectorAll('span')].find((el) => el.textContent.startsWith('next:'))?.getBoundingClientRect()
    return { identityBottom: identity.bottom, gatesTop: gates.top, gatesBottom: gates.bottom, nextTop: next?.top ?? -1 }
  })
  expect(boxes.gatesTop).toBeLessThan(boxes.identityBottom)
  expect(boxes.nextTop).toBeLessThan(boxes.identityBottom)
  expect(boxes.gatesBottom).toBeLessThanOrEqual(boxes.identityBottom + 8)

  // When everything fits, the strip does not scroll.
  const overflow = await strip.evaluate((el) => el.scrollWidth - el.clientWidth)
  expect(overflow).toBe(0)
})

test('qualification chip strip scrolls single-line at medium width and moves to a second line on mobile', async ({ page }) => {
  installMockBackend(page)
  await openProject(page)

  // Medium width: horizontal scroller, never a taller header. All chips share
  // one line, so the strip height cannot grow.
  await page.setViewportSize({ width: 900, height: 900 })
  const strip = page.getByTestId('qualification-gates')
  const medium = await strip.evaluate((el) => {
    const chips = [...el.querySelectorAll('button')]
    const tops = new Set(chips.map((chip) => Math.round(chip.getBoundingClientRect().top)))
    return { overflow: el.scrollWidth - el.clientWidth, tops: tops.size, height: el.getBoundingClientRect().height }
  })
  expect(medium.overflow).toBeGreaterThan(0)
  expect(medium.tops).toBe(1)
  expect(medium.height).toBeLessThan(30)

  // Narrow width: identity keeps the first line, chips take the second line.
  await page.setViewportSize({ width: 390, height: 800 })
  const narrow = await page.evaluate(() => {
    const identity = document.querySelector('[data-testid="project-top-module"]').getBoundingClientRect()
    const gates = document.querySelector('[data-testid="qualification-gates"]').getBoundingClientRect()
    const chip = document.querySelector('[data-testid="qualification-gate-contract"]').getBoundingClientRect()
    return { identityBottom: identity.bottom, gatesTop: gates.top, chipWidth: chip.width, chipText: chip.width > 20 }
  })
  expect(narrow.gatesTop).toBeGreaterThanOrEqual(narrow.identityBottom - 2)
  expect(narrow.chipText).toBe(true)
})

test('chip popover opens on keyboard focus and closes on Escape with focus restored', async ({ page }) => {
  installMockBackend(page)
  await openProject(page)

  const chip = page.getByTestId('qualification-gate-lint')
  await chip.focus()
  await expect(page.getByTestId('qualification-popover')).toBeVisible()
  await expect(page.getByTestId('qualification-popover')).toContainText('No lint evidence is loaded.')
  await expect(page.getByTestId('qualification-popover')).toContainText('state: not run')
  await expect(chip).toBeFocused()
  await expect(chip).toHaveAttribute('aria-expanded', 'true')

  await page.keyboard.press('Escape')
  await expect(page.getByTestId('qualification-popover')).toBeHidden()
  await expect(chip).toBeFocused()
})

test('chip popover closes on outside click and switches when another gate is selected', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  await loadState(mock, { setupslack: -2.32, fmax: 44.8 })

  await page.getByTestId('qualification-gate-lint').click()
  await expect(page.getByTestId('qualification-popover')).toBeVisible()

  // Selecting another gate swaps the popover content.
  await page.getByTestId('qualification-gate-sta').click()
  await expect(page.getByTestId('qualification-popover')).toContainText('Setup slack -2.32 ns')

  // Outside click dismisses.
  await page.mouse.click(400, 600)
  await expect(page.getByTestId('qualification-popover')).toBeHidden()
})

test('gate chips expose the full state in the accessible label without relying on color', async ({ page }) => {
  const mock = installMockBackend(page)
  await openProject(page)
  await loadState(mock, { setupslack: -2.32, fmax: 44.8 })

  const failed = page.getByTestId('qualification-gate-sta')
  await expect(failed).toHaveAttribute('aria-label', /STA: failed\./)
  await expect(failed).toContainText('failed')
  await expect(failed.locator('span').nth(0)).toBeVisible()
  const glyph = await failed.evaluate((el) => el.querySelector('[aria-hidden="true"]')?.textContent)
  expect(glyph).toBe('!')
})
