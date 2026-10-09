import { test, expect } from '@playwright/test'
import { installMockBackend } from './fixtures'

// Mistral setup wizard coverage: the header button appears when a Mistral
// entry lacks its key, the browser flow shows the sign-in URL and completes,
// and the paste-key path verifies and saves — all against mocked wizard
// endpoints, with the API key never crossing to the client.

const SIGN_IN_URL = 'https://console.mistral.ai/codestral/cli/authenticate?process_id=pw-1'

function modelsWithMistralUnavailable() {
  return [
    { key: 'kimi-k3', display_name: 'Kimi K3 (direct)', available: true, default: true, health: 'ok' },
    { key: 'mistral-vibe', display_name: 'Mistral (Vibe default)', available: false, default: false, health: 'no_key' },
    { key: 'mistral-glm-5.3', display_name: 'GLM 5.3 (Mistral hosted)', available: false, default: false, health: 'no_key' },
  ]
}

function installWizardMock(page, { pollStatuses = ['waiting', 'completed'] } = {}) {
  page.route('http://127.0.0.1:8000/api/providers/mistral/setup/state', (route) =>
    route.fulfill({ json: { key_set: false, flow: { status: 'idle', message: null, sign_in_url: null, expires_at: null } } }))
  page.route('http://127.0.0.1:8000/api/providers/mistral/setup/browser/start', (route) =>
    route.fulfill({ json: { status: 'waiting', message: null, sign_in_url: SIGN_IN_URL, expires_at: '2100-01-01T00:00:00Z' } }))
  let polls = 0
  page.route('http://127.0.0.1:8000/api/providers/mistral/setup/browser/status', (route) => {
    const status = pollStatuses[Math.min(polls, pollStatuses.length - 1)]
    polls += 1
    route.fulfill({ json: { status, message: null } })
  })
  page.route('http://127.0.0.1:8000/api/providers/mistral/setup/browser/complete', (route) =>
    route.fulfill({ json: { health: 'ok', env_path: '/project/.env' } }))
  page.route('http://127.0.0.1:8000/api/providers/mistral/setup/key', (route) =>
    route.fulfill({ json: { health: 'ok', saved: true, env_path: '/project/.env' } }))
}

async function openProject(page) {
  await page.goto('/')
  await page.getByText('Alpha', { exact: true }).first().click()
  await expect(page.getByTestId('qualification-gates')).toBeVisible()
}

test('browser sign-in flow: URL shown, poll completes, key stored server-side', async ({ page }) => {
  installMockBackend(page)
  installWizardMock(page)
  page.route('http://127.0.0.1:8000/api/models', (route) =>
    route.fulfill({ json: modelsWithMistralUnavailable() }))
  await openProject(page)

  // The setup button appears because a Mistral entry has no key.
  await page.getByTestId('mistral-setup-button').click()
  const modal = page.getByTestId('mistral-setup-modal')
  await expect(modal).toBeVisible()

  await page.getByTestId('mistral-setup-browser').click()
  await expect(page.getByTestId('mistral-setup-signin-url')).toHaveAttribute('href', SIGN_IN_URL)
  await expect(page.getByTestId('mistral-setup-signin-url')).toContainText('console.mistral.ai')
  await expect(page.getByTestId('mistral-setup-flow-status')).toContainText('waiting for sign-in', { ignoreCase: true })

  // Poll completes; the modal shows the free-probe health and the env path —
  // never a key.
  await expect(page.getByTestId('mistral-setup-done')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('mistral-setup-done')).toContainText('key verified')
  await expect(page.getByTestId('mistral-setup-done')).toContainText('/project/.env')
  await page.getByTestId('mistral-setup-finish').click()
  await expect(page.getByTestId('mistral-setup-modal')).toBeHidden()
})

test('paste-key path verifies and saves without exposing the key', async ({ page }) => {
  installMockBackend(page)
  installWizardMock(page)
  page.route('http://127.0.0.1:8000/api/models', (route) =>
    route.fulfill({ json: modelsWithMistralUnavailable() }))
  await openProject(page)

  await page.getByTestId('mistral-setup-button').click()
  await page.getByTestId('mistral-setup-paste').click()
  await page.getByTestId('mistral-setup-key-input').fill('a-pasted-key')
  await page.getByTestId('mistral-setup-submit-key').click()
  await expect(page.getByTestId('mistral-setup-done')).toBeVisible()
  await expect(page.getByTestId('mistral-setup-done')).toContainText('key verified')
  // The typed key is masked and never rendered back after completion.
  await expect(page.getByTestId('mistral-setup-done')).not.toContainText('a-pasted-key')
})

test('the setup button stays hidden when the Mistral key is already configured', async ({ page }) => {
  installMockBackend(page)
  page.route('http://127.0.0.1:8000/api/models', (route) =>
    route.fulfill({ json: [
      { key: 'kimi-k3', display_name: 'Kimi K3 (direct)', available: true, default: true, health: 'ok' },
      { key: 'mistral-vibe', display_name: 'Mistral (Vibe default)', available: true, default: false, health: 'ok' },
      { key: 'mistral-glm-5.3', display_name: 'GLM 5.3 (Mistral hosted)', available: true, default: false, health: 'ok' },
    ] }))
  await openProject(page)
  await expect(page.getByTestId('mistral-setup-button')).toBeHidden()
})

test('a denied browser sign-in surfaces the honest error state', async ({ page }) => {
  installMockBackend(page)
  installWizardMock(page, { pollStatuses: ['denied'] })
  page.route('http://127.0.0.1:8000/api/models', (route) =>
    route.fulfill({ json: modelsWithMistralUnavailable() }))
  await openProject(page)

  await page.getByTestId('mistral-setup-button').click()
  await page.getByTestId('mistral-setup-browser').click()
  await expect(page.getByTestId('mistral-setup-error-view')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('mistral-setup-error')).toContainText('denied')
})
