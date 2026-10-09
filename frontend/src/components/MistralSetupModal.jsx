import { useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import {
  mistralBrowserStart, mistralBrowserStatus, mistralBrowserComplete, mistralSubmitKey,
} from '../hooks/useAgentSocket'

// Interactive Mistral provider setup, mirroring `vibe --setup`: paste a key
// from console.mistral.ai, or sign in with the browser (PKCE). The key is
// exchanged and stored server-side; this side only ever sees the sign-in URL
// and the free-probe health of the stored credential.

const POLL_INTERVAL_MS = 3000 // upstream cadence: poll every 3 s

export default function MistralSetupModal({ onClose }) {
  const [view, setView] = useState('choose') // choose | browser | paste | done | error
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [signInUrl, setSignInUrl] = useState(null)
  const [flowStatus, setFlowStatus] = useState('idle')
  const [keyInput, setKeyInput] = useState('')
  const [result, setResult] = useState(null)
  const pollTimerRef = useRef(null)
  const viewRef = useRef(view)
  viewRef.current = view

  const stopPolling = () => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }
  useEffect(() => stopPolling, [])

  const startBrowser = async () => {
    setBusy(true)
    setError(null)
    const response = await mistralBrowserStart()
    setBusy(false)
    if (!response.ok) {
      setView('error')
      setError(response.error ?? 'failed to start browser sign-in')
      return
    }
    setSignInUrl(response.data.sign_in_url)
    setFlowStatus(response.data.status)
    setView('browser')
    schedulePoll()
  }

  const schedulePoll = () => {
    stopPolling()
    pollTimerRef.current = setTimeout(pollOnce, POLL_INTERVAL_MS)
  }

  const pollOnce = async () => {
    if (viewRef.current !== 'browser') return
    const state = await mistralBrowserStatus()
    setFlowStatus(state.status)
    if (state.status === 'waiting') {
      schedulePoll()
      return
    }
    stopPolling()
    if (state.status === 'completed') {
      setBusy(true)
      const response = await mistralBrowserComplete()
      setBusy(false)
      if (!response.ok) {
        setView('error')
        setError(response.error ?? 'exchange failed')
        return
      }
      setResult(response.data)
      setView('done')
      return
    }
    setView('error')
    setError(state.message ?? `sign-in ${state.status}`)
  }

  const submitKey = async () => {
    setBusy(true)
    setError(null)
    const response = await mistralSubmitKey(keyInput.trim())
    setBusy(false)
    if (!response.ok) {
      setView('error')
      setError(response.error ?? 'verification failed')
      return
    }
    if (response.data.health !== 'ok') {
      setError(`key rejected (${response.data.health}); nothing was saved`)
      return
    }
    setResult(response.data)
    setView('done')
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-modal="true" aria-label="Mistral provider setup" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-lg border border-gray-800 bg-[#0d0d0d] p-4 font-mono shadow-2xl"
        data-testid="mistral-setup-modal"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between border-b border-gray-800 pb-2">
          <span className="text-[11px] uppercase tracking-wider text-gray-300">Mistral provider setup</span>
          <button type="button" onClick={onClose} className="rounded p-1 text-gray-600 hover:bg-white/5 hover:text-gray-300" aria-label="Close" data-testid="mistral-setup-close">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>

        {view === 'choose' && (
          <div className="space-y-2" data-testid="mistral-setup-choose">
            <p className="text-[10px] leading-relaxed text-gray-500">
              Configure the Mistral provider (mistral-vibe / GLM 5.3 Mistral hosted). Same flow as Mistral Vibe&apos;s <code className="text-cyan-400/80">vibe --setup</code>. The key is verified with a free probe and stored in the backend&apos;s .env.
            </p>
            <button type="button" onClick={startBrowser} disabled={busy} className="w-full rounded border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-left text-[10px] text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50" data-testid="mistral-setup-browser">
              Sign in with your browser — no key pasting (PKCE)
            </button>
            <button type="button" onClick={() => setView('paste')} className="w-full rounded border border-gray-700 px-3 py-2 text-left text-[10px] text-gray-400 hover:bg-white/5" data-testid="mistral-setup-paste">
              Paste an API key from console.mistral.ai
            </button>
          </div>
        )}

        {view === 'browser' && (
          <div className="space-y-3" data-testid="mistral-setup-browser-view">
            <p className="text-[10px] text-gray-500">Open this URL in your browser and sign in:</p>
            <a
              href={signInUrl}
              target="_blank"
              rel="noreferrer"
              className="block break-all rounded border border-gray-700 bg-[#0a0a0a] p-2 text-[10px] text-cyan-400 hover:text-cyan-300"
              data-testid="mistral-setup-signin-url"
            >
              {signInUrl}
            </a>
            <div className="flex items-center gap-2 text-[10px] text-gray-500">
              <span className={`h-1.5 w-1.5 rounded-full ${flowStatus === 'waiting' ? 'animate-pulse bg-amber-400' : 'bg-gray-600'}`} />
              <span data-testid="mistral-setup-flow-status">
                {flowStatus === 'waiting' ? 'waiting for sign-in…' : flowStatus}
              </span>
              {busy && <span className="ml-auto text-amber-300">exchanging…</span>}
            </div>
            <button type="button" onClick={() => { stopPolling(); setView('choose') }} className="rounded border border-gray-800 px-2 py-1 text-[9px] text-gray-600 hover:text-gray-400">
              cancel
            </button>
          </div>
        )}

        {view === 'paste' && (
          <div className="space-y-2" data-testid="mistral-setup-paste-view">
            <label className="text-[10px] text-gray-500" htmlFor="mistral-api-key">API key</label>
            <input
              id="mistral-api-key"
              type="password"
              value={keyInput}
              onChange={(event) => setKeyInput(event.target.value)}
              placeholder="paste your Mistral API key"
              className="w-full rounded border border-gray-700 bg-[#090909] px-2 py-1.5 text-[10px] text-gray-200 outline-none focus:border-gray-500"
              data-testid="mistral-setup-key-input"
            />
            {error && <div className="text-[9px] text-red-400" data-testid="mistral-setup-error">{error}</div>}
            <div className="flex gap-2">
              <button type="button" onClick={submitKey} disabled={busy || !keyInput.trim()} className="rounded border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-[10px] text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50" data-testid="mistral-setup-submit-key">
                verify and save
              </button>
              <button type="button" onClick={() => { setError(null); setView('choose') }} className="rounded border border-gray-800 px-3 py-1.5 text-[10px] text-gray-600 hover:text-gray-400">
                back
              </button>
            </div>
          </div>
        )}

        {view === 'done' && (
          <div className="space-y-2" data-testid="mistral-setup-done">
            <div className="text-[10px] text-emerald-300">key verified (free /v1/models probe: {result?.health})</div>
            <div className="text-[10px] text-gray-500">saved to {result?.env_path ?? '.env'}</div>
            {result?.tenant_api_base && (
              <div className="text-[9px] text-gray-600">
                console-advertised tenant API base: {result.tenant_api_base} (set MISTRAL_BASE_URL={result.tenant_api_base}/v1 to use it)
              </div>
            )}
            <div className="text-[10px] leading-relaxed text-amber-300/90">
              Restart the backend (./start.sh) so it loads the key, then pick the Mistral model in the header.
            </div>
            <button type="button" onClick={onClose} className="rounded border border-gray-700 px-3 py-1.5 text-[10px] text-gray-300 hover:bg-white/5" data-testid="mistral-setup-finish">
              done
            </button>
          </div>
        )}

        {view === 'error' && (
          <div className="space-y-2" data-testid="mistral-setup-error-view">
            <div className="text-[10px] text-red-400" data-testid="mistral-setup-error">{error ?? 'setup failed'}</div>
            <button type="button" onClick={() => { setError(null); setView('choose') }} className="rounded border border-gray-700 px-3 py-1.5 text-[10px] text-gray-300 hover:bg-white/5">
              back
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
