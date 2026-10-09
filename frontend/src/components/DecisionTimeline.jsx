import { useEffect, useMemo, useRef, useState } from 'react'

// Compact run status affordance: public progress remains available without
// taking over the chat transcript.
const STATUS_TONE = {
  done: 'bg-emerald-400', passed: 'bg-emerald-400', success: 'bg-emerald-400',
  accepted: 'bg-emerald-400', failed: 'bg-red-400', failure: 'bg-red-400',
  blocked: 'bg-red-400', inconclusive: 'bg-orange-400', rejected: 'bg-red-400',
  decision: 'bg-cyan-400',
}
const LABELS = [['tokens', 'tokens'], ['context', 'context'], ['tools', 'tools'], ['rounds', 'rounds']]

function compact(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return String(value ?? '—')
  if (Math.abs(number) >= 1000000) return (number / 1000000).toFixed(1).replace(/\.0$/, '') + 'M'
  if (Math.abs(number) >= 1000) return (number / 1000).toFixed(number >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k'
  return number.toLocaleString('en-US', { maximumFractionDigits: 1 })
}

function budgetValue(budget) {
  if (!budget) return '—'
  if (budget.used !== undefined && budget.limit !== undefined) return compact(budget.used) + '/' + compact(budget.limit)
  if (budget.used !== undefined) return compact(budget.used)
  if (budget.remaining !== undefined) return compact(budget.remaining) + ' left'
  if (budget.limit !== undefined) return '≤' + compact(budget.limit)
  return '—'
}

function latestSnapshot(events) {
  const snapshot = { budgets: {}, attempt: null }
  for (const event of events) {
    if (event?.budgets) {
      for (const [name, budget] of Object.entries(event.budgets)) snapshot.budgets[name] = budget
    }
    if (event?.attempt) snapshot.attempt = event.attempt
  }
  return snapshot
}

function eventTone(event, busy) {
  if (busy && event?.status === 'running') return 'bg-amber-400'
  return STATUS_TONE[String(event?.status || '').toLowerCase()] || 'bg-gray-600'
}

function formatStep(event) {
  return (event?.phase ? event.phase + ' · ' : '') + (event?.action || 'Run update')
}

export default function DecisionTimeline({ events = [], busy = false, statusText = '', runState = null }) {
  const [pinned, setPinned] = useState(false)
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const [hoverDismissed, setHoverDismissed] = useState(false)
  const shellRef = useRef(null)
  const snapshot = useMemo(() => {
    const fromEvents = latestSnapshot(events)
    return {
      budgets: { ...fromEvents.budgets, ...(runState?.budgets || {}) },
      attempt: runState?.attempt || fromEvents.attempt,
    }
  }, [events, runState])
  const latest = events[events.length - 1]
  const open = pinned || focused || (hovered && !hoverDismissed)
  const durableLifecycle = String(runState?.lifecycle || '').toLowerCase()
  const durableEvent = durableLifecycle ? { status: durableLifecycle } : null
  const dotTone = busy ? 'bg-amber-400' : eventTone(durableEvent || latest, false)
  const status = busy ? 'RUNNING' : (durableLifecycle || latest?.status || 'IDLE').toUpperCase()
  const displayText = statusText || runState?.terminal_reason || runState?.reason || latest?.action || 'No run activity yet'
  const panelId = 'agent-run-status'

  useEffect(() => {
    if (!open) return undefined
    const dismissOutside = (event) => {
      const target = event.target
      if (!shellRef.current?.contains(target)) {
        setPinned(false)
        setHovered(false)
        setFocused(false)
        setHoverDismissed(true)
      }
    }
    document.addEventListener('pointerdown', dismissOutside, true)
    const dismissEscape = (event) => {
      if (event.key === 'Escape') {
        setPinned(false)
        setHovered(false)
        setFocused(false)
        setHoverDismissed(true)
      }
    }
    document.addEventListener('keydown', dismissEscape)
    return () => {
      document.removeEventListener('pointerdown', dismissOutside, true)
      document.removeEventListener('keydown', dismissEscape)
    }
  }, [open])

  return (
    <div ref={shellRef} className="relative flex items-center" onMouseEnter={() => { setHovered(true); setHoverDismissed(false) }} onMouseLeave={() => setHovered(false)}>
      <button
        type="button"
        aria-label={open ? 'Hide run status' : 'Show run status'}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => { setHoverDismissed(false); setPinned((value) => !value) }}
        onKeyDown={(event) => { if (event.key === 'Escape') setPinned(false) }}
        onFocus={() => { setHoverDismissed(false); setFocused(true) }}
        onBlur={(event) => {
          if (!event.currentTarget.parentElement?.contains(event.relatedTarget)) setFocused(false)
        }}
        className="group flex h-7 w-7 items-center justify-center rounded-full border border-transparent transition-colors hover:border-gray-700 focus-visible:border-gray-500 focus-visible:outline-none"
      >
        <span className={'h-2.5 w-2.5 rounded-full transition-transform duration-200 group-hover:scale-150 ' + dotTone + (busy ? ' animate-pulse' : '')} />
      </button>

      {open && (
        <section
          id={panelId}
          role="status"
          aria-live="polite"
          className="absolute right-0 top-full z-50 mt-1 w-72 max-h-[min(28rem,calc(100vh-8rem))] overflow-y-auto rounded-lg border border-gray-700/90 bg-[#101010]/[.98] p-3 shadow-2xl shadow-black/50 backdrop-blur-sm"
          onMouseEnter={() => setHovered(true)}
        >
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[9px] font-mono uppercase tracking-[.16em] text-gray-600">run status</div>
              <div data-testid="run-status-summary" className="mt-1 text-xs font-mono text-gray-200 break-words">{displayText}</div>
            </div>
            <span className={'shrink-0 text-[9px] font-mono uppercase tracking-wider ' + (busy ? 'text-amber-300' : latest ? 'text-gray-400' : 'text-gray-600')}>{status}</span>
          </div>

          <div className="mt-3 grid grid-cols-2 gap-1.5">
            {LABELS.map(([name, label]) => (
              <div key={name} className="rounded border border-gray-800 bg-[#0b0b0b] px-2 py-1.5">
                <div className="text-[9px] font-mono uppercase tracking-wider text-gray-600">{label}</div>
                <div className="mt-0.5 text-[11px] font-mono text-gray-300">{budgetValue(snapshot.budgets[name])}</div>
              </div>
            ))}
          </div>

          {snapshot.attempt && (
            <div className="mt-2 text-[9px] font-mono text-gray-500">
              {'attempt ' + (snapshot.attempt.current ?? '—') + (snapshot.attempt.limit !== undefined ? '/' + snapshot.attempt.limit : '')}
            </div>
          )}

          {events.length > 0 && (
            <div className="mt-3 border-t border-gray-800 pt-2">
              <div className="mb-1.5 text-[9px] font-mono uppercase tracking-[.16em] text-gray-600">recent steps</div>
              <div className="space-y-1">
                {events.slice(-4).reverse().map((event) => (
                  <div key={event.id} className="flex min-w-0 items-center gap-2 text-[10px] font-mono">
                    <span className={'h-1.5 w-1.5 shrink-0 rounded-full ' + eventTone(event, busy && event === latest)} />
                    <span className="min-w-0 flex-1 truncate text-gray-400">{formatStep(event)}</span>
                    <span className="shrink-0 text-[9px] uppercase tracking-wider text-gray-600">{event.status}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="mt-3 text-[9px] font-mono text-gray-700">Click the dot to keep this panel open.</div>
        </section>
      )}
    </div>
  )
}
