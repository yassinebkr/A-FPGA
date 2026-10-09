import { useState } from 'react'
import { Check, ChevronDown, ChevronRight, CircleQuestionMark, LockKeyhole, Pencil, RotateCcw, X } from 'lucide-react'
import {
  CONTRACT_FIELD_REGISTRY,
  CONTRACT_SECTION_REGISTRY,
  CONTRACT_STATE_LABELS,
  CONTRACT_STATES,
  contractFieldValue,
  contractGate,
} from '@/lib/designContract'

const STATUS_TONE = {
  [CONTRACT_STATES.DRAFT]: 'border-gray-700 text-gray-400 bg-gray-500/10',
  [CONTRACT_STATES.NEEDS_DECISION]: 'border-amber-500/40 text-amber-300 bg-amber-500/10',
  [CONTRACT_STATES.READY]: 'border-emerald-500/40 text-emerald-300 bg-emerald-500/10',
  [CONTRACT_STATES.REVISED]: 'border-cyan-500/40 text-cyan-300 bg-cyan-500/10',
  [CONTRACT_STATES.LOCKED]: 'border-violet-500/40 text-violet-300 bg-violet-500/10',
}

const asText = (value) => {
  if (value === null || value === undefined || value === '') return ''
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (Array.isArray(value)) return value.map(asText).filter(Boolean).join(', ')
  if (typeof value === 'object') return Object.entries(value).map(([key, item]) => `${key}: ${asText(item)}`).join(' · ')
  return String(value)
}

function fieldValue(contract, section, field) {
  return contractFieldValue(contract, section, field)
}

function displayValue(value, descriptor) {
  const text = asText(value)
  return text && descriptor?.unit ? `${text} ${descriptor.unit}` : text
}

function contractBrief(contract) {
  const top = asText(contract.top_module || contractFieldValue(contract, 'interface', 'top_module'))
  const target = asText(contract.technology || contractFieldValue(contract, 'target', 'technology'))
  const clock = asText(contract.clock_target_mhz || contractFieldValue(contract, 'timing', 'clock_target_mhz'))
  return [top && `top ${top}`, target && `target ${target}`, clock && `${clock} MHz`].filter(Boolean).join(' · ')
}

function initialDraft(contract) {
  const draft = {}
  for (const section of CONTRACT_SECTION_REGISTRY) {
    for (const field of section.fields) {
      const value = fieldValue(contract, section.key, field)
      if (value !== undefined) draft[field] = value
    }
  }
  return draft
}

function ContractStatus({ status }) {
  return (
    <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-wider ${STATUS_TONE[status] || STATUS_TONE.draft}`}>
      {CONTRACT_STATE_LABELS[status] || 'Draft'}
    </span>
  )
}

function ContractField({ contract, section, field }) {
  const descriptor = CONTRACT_FIELD_REGISTRY[field]
  const value = fieldValue(contract, section, field)
  if (!descriptor || value === undefined || value === null || value === '') return null
  return (
    <div className="min-w-0 rounded border border-gray-800/80 bg-[#0b0b0b] px-2 py-1.5">
      <div className="truncate text-[9px] font-mono uppercase tracking-wider text-gray-600">{descriptor.label}</div>
      <div className="mt-0.5 break-words text-[10px] font-mono text-gray-300">{displayValue(value, descriptor)}</div>
    </div>
  )
}

// The verification spec is a nested object; dumping it through asText
// produced an unreadable wall of key/value lines. Scalars become chips,
// collections become counts, and the raw form stays one click away.
function VerificationSummary({ value }) {
  const entries = value && typeof value === 'object' && !Array.isArray(value) ? value : null
  if (!entries) return null
  const scalars = Object.entries(entries).filter(([, v]) =>
    v === null || v === undefined || typeof v !== 'object')
  const collections = Object.entries(entries).filter(([, v]) =>
    Array.isArray(v) || typeof v === 'object')
  return (
    <div className="rounded border border-gray-800/80 bg-[#0b0b0b] p-2">
      <div className="flex flex-wrap gap-1.5">
        {scalars.filter(([, v]) => v !== null && v !== undefined && v !== '').map(([key, v]) => (
          <span key={key} className="rounded border border-gray-800 px-1.5 py-0.5 text-[9px] font-mono text-gray-400">
            <span className="text-gray-600">{key}:</span> {asText(v)}
          </span>
        ))}
        {collections.map(([key, v]) => {
          const count = Array.isArray(v) ? v.length : Object.keys(v).length
          return (
            <span key={key} className="rounded border border-gray-800 px-1.5 py-0.5 text-[9px] font-mono text-gray-500">
              <span className="text-gray-600">{key}:</span> {count} entr{count === 1 ? 'y' : 'ies'}
            </span>
          )
        })}
      </div>
      <details className="mt-1.5">
        <summary className="cursor-pointer list-none text-[9px] font-mono uppercase tracking-wider text-gray-600 hover:text-gray-400 [&::-webkit-details-marker]:hidden">Raw verification spec</summary>
        <pre className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-words border-t border-gray-800 pt-1.5 text-[9px] leading-relaxed text-gray-600">{JSON.stringify(value, null, 2)}</pre>
      </details>
    </div>
  )
}

function ContractEditor({ contract, onCancel, onRevise }) {
  const [draft, setDraft] = useState(() => initialDraft(contract))

  const update = (field, value, input) => {
    let next = value
    if (input === 'number') next = value === '' ? '' : Number(value)
    if (input === 'boolean') next = value === true || value === 'true'
    setDraft((current) => ({ ...current, [field]: next }))
  }

  const submit = () => {
    const patch = {}
    for (const section of CONTRACT_SECTION_REGISTRY) {
      for (const field of section.fields) {
        if (draft[field] === undefined) continue
        const current = fieldValue(contract, section.key, field)
        if (String(draft[field]) !== String(current ?? '')) patch[field] = draft[field]
      }
    }
    onRevise({
      contract_id: contract.id,
      base_revision: contract.revision,
      patch,
    })
  }

  return (
    <div className="mt-2 rounded-md border border-cyan-500/20 bg-[#0a1010] p-2.5">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-[9px] font-mono uppercase tracking-[.16em] text-cyan-300/70">Revise contract</div>
        <button type="button" onClick={onCancel} className="rounded p-1 text-gray-600 hover:bg-gray-800 hover:text-gray-300" aria-label="Close contract editor">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {CONTRACT_SECTION_REGISTRY.map((section) => {
          const fields = section.fields.filter((field) => CONTRACT_FIELD_REGISTRY[field])
          return (
            <fieldset key={section.key} className="min-w-0 rounded border border-gray-800 p-2">
              <legend className="px-1 text-[9px] font-mono uppercase tracking-wider text-gray-600">{section.label}</legend>
              <div className="space-y-1.5">
                {fields.map((field) => {
                  const descriptor = CONTRACT_FIELD_REGISTRY[field]
                  const value = draft[field] ?? ''
                  if (descriptor.input === 'select') {
                    return (
                      <label key={field} className="block text-[9px] font-mono text-gray-500">
                        {descriptor.label}
                        <select value={value} onChange={(event) => update(field, event.target.value, descriptor.input)} className="mt-1 w-full rounded border border-gray-800 bg-[#111] px-2 py-1 text-[10px] text-gray-300 focus:border-cyan-600/60 focus:outline-none">
                          <option value="">—</option>
                          {descriptor.options.map((option) => <option key={option} value={option}>{option}</option>)}
                        </select>
                      </label>
                    )
                  }
                  if (descriptor.input === 'boolean') {
                    return (
                      <label key={field} className="flex items-center gap-2 text-[9px] font-mono text-gray-500">
                        <input type="checkbox" checked={Boolean(value)} onChange={(event) => update(field, event.target.checked, descriptor.input)} className="accent-cyan-500" />
                        {descriptor.label}
                      </label>
                    )
                  }
                  return (
                    <label key={field} className="block text-[9px] font-mono text-gray-500">
                      {descriptor.label}{descriptor.unit ? ` (${descriptor.unit})` : ''}
                      <input type={descriptor.input} value={value} onChange={(event) => update(field, event.target.value, descriptor.input)} className="mt-1 w-full rounded border border-gray-800 bg-[#111] px-2 py-1 text-[10px] text-gray-300 placeholder-gray-700 focus:border-cyan-600/60 focus:outline-none" />
                    </label>
                  )
                })}
              </div>
            </fieldset>
          )
        })}
      </div>
      <div className="mt-2 flex items-center justify-end gap-2">
        <button type="button" onClick={onCancel} className="rounded border border-gray-800 px-2 py-1 text-[9px] font-mono uppercase tracking-wider text-gray-500 hover:border-gray-600 hover:text-gray-300">Cancel</button>
        <button type="button" onClick={submit} className="rounded border border-cyan-500/40 bg-cyan-500/10 px-2 py-1 text-[9px] font-mono uppercase tracking-wider text-cyan-300 hover:bg-cyan-500/20">Send revision</button>
      </div>
    </div>
  )
}

function DecisionPrompt({ decision, answer: authoritativeAnswer, pending, onAnswer, locked }) {
  const answer = authoritativeAnswer ?? pending ?? decision.answer ?? decision.value
  return (
    <div className="rounded border border-amber-500/20 bg-amber-500/[.04] px-2.5 py-2">
      <div className="flex items-start gap-2">
        <CircleQuestionMark className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-300/80" />
        <div className="min-w-0 flex-1">
          <div className="text-[10px] leading-relaxed text-gray-300">{decision.prompt}</div>
          {decision.rationale && <div className="mt-0.5 text-[9px] font-mono text-gray-600">{asText(decision.rationale)}</div>}
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {decision.options.length === 0 ? (
              <span className="text-[9px] font-mono text-gray-600">Answer in chat{answer ? ` · ${asText(answer)}` : ''}</span>
            ) : decision.options.map((option, index) => {
              const value = option.value ?? option.id ?? option.label
              const selected = String(value) === String(answer)
              return (
                <button key={`${decision.id}-${index}`} type="button" disabled={locked} onClick={() => onAnswer(decision, value)} className={`rounded border px-2 py-1 text-[9px] font-mono transition-colors ${selected ? 'border-amber-400/60 bg-amber-400/10 text-amber-200' : 'border-gray-800 text-gray-500 hover:border-amber-500/40 hover:text-gray-300'} disabled:cursor-not-allowed disabled:opacity-50`}>
                  {option.label || value}{selected && pending !== undefined ? ' · pending' : ''}
                </button>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}

export default function DesignContract({ contract, metrics = {}, onAction, busy = false, isRunning = false }) {
  const [expanded, setExpanded] = useState(false)
  const [editing, setEditing] = useState(false)
  const [pendingAnswers, setPendingAnswers] = useState({})
  const [pendingAnswersRevision, setPendingAnswersRevision] = useState(null)
  const [pendingAction, setPendingAction] = useState(null)
  if (!contract) return null

  const status = contract.status || CONTRACT_STATES.DRAFT
  const gate = contractGate(contract, { busy, isRunning })
  const locked = gate.locked
  const unresolved = gate.unresolved
  const actionPending = pendingAction?.revision === contract.revision
  const visibleSections = CONTRACT_SECTION_REGISTRY.map((section) => ({
    ...section,
    fields: section.fields.filter((field) => fieldValue(contract, section.key, field) !== undefined),
  })).filter((section) => section.fields.length > 0)

  const send = (action, payload = {}) => onAction?.(action, { ...payload, contract_id: contract.id, revision: contract.revision })
  const markAction = (action) => {
    const accepted = send(action, action === 'contract.approve_and_build' ? { intent: 'approve_and_build' } : {})
    if (accepted !== false) {
      setPendingAction({ action, revision: contract.revision })
      // Approving resumes the build; the expanded detail panel has served its
      // purpose and must not keep covering the console.
      if (action === 'contract.approve_and_build') setExpanded(false)
    }
    return accepted
  }
  const answer = (decision, value) => {
    if (actionPending || locked) return
    const accepted = send('contract.answer_decision', { decision_id: decision.id, answer: value })
    if (accepted === false) return
    setPendingAnswersRevision(contract.revision)
    setPendingAnswers((current) => ({ ...current, [decision.id]: value }))
    setPendingAction({ action: 'contract.answer_decision', revision: contract.revision })
  }

  return (
    <section className="shrink-0 border-b border-gray-800 bg-[#101010] px-3 py-2.5" aria-label="Design contract">
      <div className="flex min-w-0 items-center gap-2">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${locked ? 'bg-violet-400' : unresolved ? 'bg-amber-400' : 'bg-emerald-400'}`} />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <span className="shrink-0 text-[9px] font-mono uppercase tracking-[.16em] text-gray-500">Design Contract</span>
            <ContractStatus status={status} />
            {contract.revision !== null && contract.revision !== undefined && <span className="truncate text-[9px] font-mono text-gray-700">rev {contract.revision}</span>}
          </div>
          <div className="mt-1 truncate text-[10px] font-mono text-gray-300">
            {asText(contract.top_module || fieldValue(contract, 'interface', 'top_module')) || 'Implementation intent'}
            {(contract.technology || fieldValue(contract, 'target', 'technology')) && <span className="text-gray-600"> · {asText(contract.technology || fieldValue(contract, 'target', 'technology'))}</span>}
            {(contract.clock_target_mhz || fieldValue(contract, 'timing', 'clock_target_mhz')) && <span className="text-gray-600"> · {asText(contract.clock_target_mhz || fieldValue(contract, 'timing', 'clock_target_mhz'))} MHz</span>}
          </div>
        </div>
        {!locked && (
          <button type="button" disabled={actionPending} onClick={() => setEditing((value) => !value)} className="shrink-0 rounded border border-gray-800 p-1.5 text-gray-600 hover:border-gray-600 hover:text-gray-300" aria-label={editing ? 'Close contract editor' : 'Edit design contract'}>
            <Pencil className="h-3 w-3" />
          </button>
        )}
        {!locked && (
          <button type="button" disabled={!gate.canApprove || actionPending} onClick={() => markAction('contract.approve_and_build')} className="shrink-0 rounded border border-emerald-500/30 bg-emerald-500/[.06] px-2 py-1.5 text-[9px] font-mono uppercase tracking-wider text-emerald-300 hover:bg-emerald-500/15 disabled:cursor-not-allowed disabled:border-gray-800 disabled:bg-transparent disabled:text-gray-700" title={unresolved ? 'Answer required decisions first' : actionPending ? 'Waiting for the backend contract state' : 'Approve the contract and start the saved request'}>
            <Check className="mr-1 inline h-3 w-3" />Approve &amp; build
          </button>
        )}
        {locked && <LockKeyhole className="h-3.5 w-3.5 shrink-0 text-violet-300/70" aria-label="Contract locked" />}
        <button type="button" onClick={() => setExpanded((value) => !value)} className="shrink-0 rounded p-1 text-gray-600 hover:bg-gray-800 hover:text-gray-300" aria-label={expanded ? 'Collapse design contract' : 'Expand design contract'} aria-expanded={expanded}>
          {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        </button>
      </div>

      {!locked && gate.reason && (
        <div className="mt-2 flex items-center gap-1.5 border-l border-amber-500/40 pl-2 text-[9px] font-mono text-amber-200/70" role="status">
          <span className="h-1 w-1 shrink-0 rounded-full bg-amber-400" />
          <span>Paused · {gate.reason}</span>
        </div>
      )}
      {locked && gate.building && (
        <div className="mt-2 flex items-center gap-1.5 border-l border-emerald-500/40 pl-2 text-[9px] font-mono text-emerald-200/70" role="status">
          <span className="h-1 w-1 shrink-0 rounded-full bg-emerald-400 animate-pulse" />
          <span>Build running from the approved contract</span>
        </div>
      )}

      {expanded && (
        <div className="mt-2.5 max-h-[min(28rem,45vh)] space-y-2 overflow-y-auto overscroll-contain pr-1">
          {contractBrief(contract) && (
            <div className="rounded border border-gray-800/80 bg-[#0b0b0b] px-2.5 py-1.5 text-[10px] font-mono text-gray-400">
              <span className="mr-2 text-[9px] uppercase tracking-[.16em] text-gray-600">Contract brief</span>
              {contractBrief(contract)}
            </div>
          )}
          {contract.revisionDiff?.length > 0 && (
            <div className="rounded border border-amber-500/25 bg-amber-500/[.03] px-2.5 py-1.5">
              <div className="mb-1 text-[9px] font-mono uppercase tracking-[.16em] text-amber-300/80">Revision {contract.revision} · approval required</div>
              <div className="space-y-1 text-[10px] font-mono text-gray-300">
                {contract.revisionDiff.map((change) => <div key={`${change.section}-${change.field}`}><span className="text-gray-600">{change.label}:</span> <span className="text-gray-500">{String(change.previous)}</span> <span className="px-1 text-amber-300">→</span> <span className="text-amber-200">{String(change.next)}</span></div>)}
              </div>
            </div>
          )}
          {(metrics.setupslack !== undefined || metrics.fmax !== undefined) && (
            <div className="rounded border border-gray-800/80 bg-[#0b0b0b] px-2.5 py-1.5">
              <div className="mb-1 text-[9px] font-mono uppercase tracking-[.16em] text-gray-600">Measured timing</div>
              <div className="grid grid-cols-2 gap-1.5 text-[10px] font-mono">
                <div className="rounded border border-gray-800 px-2 py-1 text-gray-300">Fmax <span className="text-gray-100">{metrics.fmax ?? '—'} MHz</span></div>
                <div className={`rounded border px-2 py-1 ${Number(metrics.setupslack) < 0 ? 'border-red-500/30 text-red-300' : 'border-gray-800 text-gray-300'}`}>Slack <span className="text-gray-100">{metrics.setupslack ?? '—'} ns</span></div>
              </div>
              {Number(metrics.setupslack) < 0 && <div className="mt-1 text-[9px] font-mono text-amber-300/80">Paused · measured timing misses the 50 MHz target; the prior architecture timing assumption is invalid.</div>}
            </div>
          )}
          {contract.summary && (
            <details className="rounded border border-gray-800/80 bg-[#0b0b0b] px-2.5 py-1.5">
              <summary className="cursor-pointer list-none text-[9px] font-mono uppercase tracking-wider text-gray-500 [&::-webkit-details-marker]:hidden">
                Architecture rationale <span className="ml-1 text-gray-700">(generated context)</span>
              </summary>
              <div className="mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap break-words border-t border-gray-800 pt-2 text-[10px] leading-relaxed text-gray-500">{asText(contract.summary)}</div>
            </details>
          )}
          {contract.pendingRequest && (
            <details className="rounded border border-gray-800/80 bg-[#0b0b0b] px-2.5 py-1.5">
              <summary className="cursor-pointer list-none text-[9px] font-mono uppercase tracking-wider text-gray-600 [&::-webkit-details-marker]:hidden">Saved request · approval will resume this build</summary>
              <div className="mt-2 border-t border-gray-800 pt-2 text-[10px] leading-relaxed text-gray-500">{asText(contract.pendingRequest)}</div>
            </details>
          )}
          {contract.decisions.length > 0 && (
            <div className="space-y-1.5">
              <div className="text-[9px] font-mono uppercase tracking-[.16em] text-amber-300/70">Material decisions</div>
              {contract.decisions.map((decision) => <DecisionPrompt key={decision.id} decision={decision} answer={contract.answers?.[decision.id]} pending={pendingAnswersRevision === contract.revision ? pendingAnswers[decision.id] : undefined} onAnswer={answer} locked={locked || actionPending} />)}
            </div>
          )}
          {contract.inferredDefaults.length > 0 && (
            <details className="rounded border border-gray-800/80 bg-[#0b0b0b] px-2.5 py-1.5">
              <summary className="flex cursor-pointer list-none items-center gap-2 text-[9px] font-mono uppercase tracking-wider text-gray-600 [&::-webkit-details-marker]:hidden">
                <RotateCcw className="h-3 w-3" />Inferred defaults <span className="text-gray-700">({contract.inferredDefaults.length})</span>
              </summary>
              <div className="mt-2 space-y-1 border-t border-gray-800 pt-2">
                {contract.inferredDefaults.map((item, index) => <div key={index} className="text-[10px] font-mono leading-relaxed text-gray-500">{asText(item.label ?? item.name ?? item.key ?? item)}{item.value !== undefined && <span className="text-gray-300">: {asText(item.value)}</span>}{item.reason && <span className="text-gray-700"> · {asText(item.reason)}</span>}</div>)}
              </div>
            </details>
          )}
          <div className="grid gap-1.5 sm:grid-cols-2">
            {visibleSections.map((section) => (
              <div key={section.key} className="rounded border border-gray-800/80 p-2">
                <div className="mb-1.5 text-[9px] font-mono uppercase tracking-wider text-gray-600">{section.label}</div>
                {section.key === 'verification' ? (
                  <VerificationSummary value={fieldValue(contract, 'verification', 'verification')} />
                ) : (
                  <div className="grid gap-1.5 grid-cols-2">
                    {section.fields.map((field) => <ContractField key={`${section.key}-${field}`} contract={contract} section={section.key} field={field} />)}
                  </div>
                )}
              </div>
            ))}
          </div>
          {editing && <ContractEditor contract={contract} onCancel={() => setEditing(false)} onRevise={(payload) => {
            const accepted = send('contract.revise', payload)
            if (accepted !== false) setPendingAction({ action: 'contract.revise', revision: contract.revision })
            setEditing(false)
          }} />}
        </div>
      )}
    </section>
  )
}

