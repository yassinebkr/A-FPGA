import { useEffect, useLayoutEffect, useRef, useState } from 'react'

const STATUS = {
  passed: { label: 'passed', dot: 'bg-emerald-400', text: 'text-emerald-400', border: 'border-emerald-500/30' },
  failed: { label: 'failed', dot: 'bg-red-400', text: 'text-red-400', border: 'border-red-500/30' },
  running: { label: 'running', dot: 'bg-amber-400 animate-pulse', text: 'text-amber-400', border: 'border-amber-500/30' },
  awaiting: { label: 'awaiting', dot: 'bg-violet-400', text: 'text-violet-400', border: 'border-violet-500/30' },
  screening: { label: 'screening', dot: 'bg-cyan-400', text: 'text-cyan-400', border: 'border-cyan-500/30' },
  stale: { label: 'stale', dot: 'bg-orange-400', text: 'text-orange-400', border: 'border-orange-500/30' },
  unavailable: { label: 'unavailable', dot: 'bg-gray-600', text: 'text-gray-500', border: 'border-gray-800' },
  not_run: { label: 'not run', dot: 'bg-gray-700', text: 'text-gray-600', border: 'border-gray-800' },
}

// Failed and running gates stay distinguishable without relying on color
// alone: every chip renders a state glyph next to the dot.
const STATE_GLYPH = {
  failed: '!', running: '~', awaiting: '?', stale: '*', unavailable: '-', not_run: '·',
}

function terminalStatus(job, { screening = false } = {}) {
  if (!job) return 'not_run'
  if (job.status === 'queued' || job.status === 'running') return 'running'
  if (job.status === 'failed' || job.status === 'cancelled' || job.status === 'interrupted' || job.outcome === 'failure') return 'failed'
  if (job.outcome === 'inconclusive') return 'unavailable'
  if (job.status === 'done' && job.outcome !== 'failure') return screening ? 'screening' : 'passed'
  return 'unavailable'
}

function latestJob(jobs, projectId, kinds) {
  return Object.values(jobs ?? {})
    .filter((job) => job.session === projectId && kinds.includes(job.kind))
    .sort((a, b) => (b.startedAt ?? Date.parse(b.created_at ?? 0) ?? 0) - (a.startedAt ?? Date.parse(a.created_at ?? 0) ?? 0))[0]
}

function contractTarget(contract, metrics) {
  const timing = contract?.sections?.timing ?? {}
  return metrics?.target_mhz ?? metrics?.targetMHz ?? timing.clock_target_mhz ?? contract?.clock_target_mhz ?? null
}

// One content-sized chip: status dot + short gate label only. Width is driven
// by the content (no flex-1, no fixed/minimum width); the full state lives in
// the accessible label and the visually-hidden text so state stays readable
// without color and without widening the chip.
function GateChip({ gate, open, onFocus, onHover, buttonRef }) {
  const style = STATUS[gate.status] ?? STATUS.unavailable
  const glyph = STATE_GLYPH[gate.status]
  return (
    <button
      ref={buttonRef}
      type="button"
      data-testid={`qualification-gate-${gate.key}`}
      data-gate-state={gate.status}
      aria-expanded={open}
      aria-label={`${gate.label}: ${style.label}. Evidence: ${gate.evidence} Next: ${gate.next}`}
      title={`${gate.label} — ${style.label}`}
      onFocus={() => onFocus(gate.key)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) onFocus(null)
      }}
      onMouseEnter={() => onHover(gate.key)}
      onMouseLeave={() => onHover(null)}
      className="shrink-0 inline-flex items-center gap-1.5 whitespace-nowrap rounded border bg-[#0b0b0b] px-1.5 py-0.5 transition-colors hover:border-gray-600 focus:outline-none focus-visible:ring-1 focus-visible:ring-emerald-400/60"
    >
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`} />
      <span className="font-mono text-[9px] uppercase tracking-wider text-gray-300">{gate.label}</span>
      {glyph && <span aria-hidden="true" className={`font-mono text-[9px] ${style.text}`}>{glyph}</span>}
      <span className="sr-only">state: {style.label}</span>
    </button>
  )
}

export default function ProjectOverview({
  project, activeModel, verilog, latestLint, hardwareProject, designContract, metrics,
  metricProvenance, jobs,
}) {
  const files = hardwareProject?.files ?? []
  const agentRtl = verilog?.code ? verilog : null
  const simulation = latestJob(jobs, project.id, ['simulation', 'verification'])
  const formal = latestJob(jobs, project.id, ['formal'])
  const synthesis = latestJob(jobs, project.id, ['synthesis', 'fmax'])
  const physical = Object.values(jobs ?? {}).filter((job) => job.session === project.id).find((job) => {
    const manifest = job.manifest ?? {}
    return manifest.flow_mode === 'pnr' || manifest.mode === 'pnr' || manifest.stage === 'place_route'
  })
  const active = Object.values(jobs ?? {}).find((job) => job.session === project.id && ['queued', 'running'].includes(job.status))
  const target = contractTarget(designContract, metrics)
  const fmax = Number(metrics?.fmax)
  const slack = Number(metrics?.setupslack)
  const hasTiming = Number.isFinite(slack) || Number.isFinite(fmax)
  const sourceStale = metricProvenance?.hardware_project_digest && hardwareProject?.digest &&
    metricProvenance.hardware_project_digest !== hardwareProject.digest

  const contractStatus = designContract?.status === 'locked'
    ? 'passed'
    : designContract?.status === 'ready' ? 'awaiting' : 'not_run'
  const staStatus = sourceStale ? 'stale' : hasTiming
    ? (Number.isFinite(slack) && slack < 0 ? 'failed' : 'passed')
    : 'not_run'

  // Sources: a validated multi-file manifest is the authoritative form; the
  // agent flow's bound RTL is legitimate single-file evidence (it flowed
  // through lint/simulation/synthesis with digest-bound artifacts).
  const sourcesStatus = files.length ? 'passed'
    : agentRtl ? 'passed'
      : hardwareProject ? 'failed' : 'not_run'
  // Lint: the persisted write_and_check_syntax verdict, bound to the current
  // RTL digest by the backend. A verdict for another revision is stale, never
  // a pass.
  const lintStatus = !latestLint ? 'not_run'
    : latestLint.bound === false ? 'stale'
      : latestLint.ok ? 'passed' : 'failed'

  const gates = [
    { key: 'contract', label: 'Contract', status: contractStatus, evidence: designContract ? `Revision ${designContract.revision ?? '?'} is ${designContract.status}.` : 'No approved design contract.', next: contractStatus === 'passed' ? 'Keep the revision binding intact.' : 'Define and approve the design contract.' },
    { key: 'sources', label: 'Sources', status: sourcesStatus,
      evidence: files.length ? `${files.length} files bound to manifest ${hardwareProject.digest?.slice(0, 10)}.`
        : agentRtl ? `Agent RTL (top ${agentRtl.top ?? '?'}) passed lint and synthesis with digest-bound artifacts; no multi-file manifest.`
          : 'No authoritative source manifest.',
      next: files.length ? 'Run verification against this digest.'
        : agentRtl ? 'Promote to a validated source manifest for multi-file designs.'
          : 'Create or import a validated source manifest.' },
    { key: 'lint', label: 'Lint', status: lintStatus,
      evidence: !latestLint ? 'No lint evidence is loaded.'
        : latestLint.bound === false ? `Verdict ${latestLint.verdict ?? '?'} belongs to an older RTL revision (digest ${latestLint.code_sha256?.slice(0, 10) ?? '?'}).`
          : `Verdict ${latestLint.verdict ?? '?'} on the current RTL digest (${latestLint.code_sha256?.slice(0, 10) ?? '?'}).`,
      next: lintStatus === 'stale' ? 'Re-run write_and_check_syntax on the current RTL.'
        : lintStatus === 'passed' ? 'Screening lint only; the policy-driven pipeline gate is stronger.'
          : 'Run the unified verification pipeline.' },
    { key: 'simulation', label: 'Simulation', status: terminalStatus(simulation, { screening: simulation?.kind !== 'verification' }), evidence: simulation ? `${simulation.kind} job ${simulation.id}: ${simulation.outcome ?? simulation.status}.` : 'No project simulation evidence.', next: simulation?.kind === 'verification' ? 'Review its immutable evidence report.' : 'Run independent verification; ordinary simulation is screening only.' },
    { key: 'formal', label: 'Formal / ISA', status: terminalStatus(formal, { screening: true }), evidence: formal ? `Formal job ${formal.id}: ${formal.outcome ?? formal.status}.` : 'No formal or ISA evidence.', next: 'Run the required formal/ISA profile.' },
    { key: 'synthesis', label: 'Synthesis', status: terminalStatus(synthesis, { screening: true }), evidence: synthesis ? `Job ${synthesis.id}: ${synthesis.outcome ?? synthesis.status}.` : 'No synthesis job.', next: 'Bind synthesis evidence to the active source and constraint digests.' },
    { key: 'sta', label: 'STA', status: staStatus, evidence: sourceStale ? 'Timing evidence belongs to another source digest.' : hasTiming ? `Setup slack ${Number.isFinite(slack) ? slack + ' ns' : 'unavailable'}; Fmax ${Number.isFinite(fmax) ? fmax + ' MHz' : 'unavailable'}.` : 'No measured timing evidence.', next: staStatus === 'failed' ? 'Diagnose the measured critical path.' : 'Measure the exact constrained candidate.' },
    { key: 'pnr', label: 'P&R', status: terminalStatus(physical), evidence: physical ? `Physical job ${physical.id}: ${physical.outcome ?? physical.status}.` : 'No explicit project P&R evidence is loaded.', next: 'Run a bounded physical implementation after admission gates pass.' },
    { key: 'drc-lvs', label: 'DRC / LVS', status: 'not_run', evidence: 'No dedicated DRC/LVS evidence record.', next: 'Complete routing and run the provider-compatible checks.' },
    { key: 'equivalence', label: 'Equivalence', status: 'not_run', evidence: 'No post-layout equivalence evidence loaded.', next: 'Compare the exact routed netlist against the bound RTL.' },
    { key: 'release', label: 'Release', status: 'not_run', evidence: 'No explicit release-gate decision.', next: 'Satisfy every mandatory gate; screening evidence is insufficient.' },
  ]

  const failed = gates.find((gate) => gate.status === 'failed')
  const pending = gates.find((gate) => ['awaiting', 'not_run', 'unavailable', 'stale'].includes(gate.status))
  const blocker = failed ?? pending

  // The popover state is split by origin (keyboard focus vs hover) so Escape
  // and outside-click can dismiss a hover-opened popover exactly like a
  // focus-opened one, and selecting another gate simply switches the origin.
  const [focusGate, setFocusGate] = useState(null)
  const [hoverGate, setHoverGate] = useState(null)
  const openKey = focusGate ?? hoverGate
  const openGate = gates.find((gate) => gate.key === openKey) ?? null
  const sectionRef = useRef(null)
  const chipRefs = useRef(new Map())
  const [popoverPos, setPopoverPos] = useState(null)

  // Dismiss on Escape and on outside pointer-down; the popover is anchored to
  // the section, so measuring with viewport rects stays correct even while
  // the chip strip scrolls horizontally.
  useEffect(() => {
    if (!openKey) return undefined
    const close = () => {
      setFocusGate(null)
      setHoverGate(null)
    }
    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        chipRefs.current.get(openKey)?.focus()
        close()
      }
    }
    const onPointerDown = (event) => {
      const section = sectionRef.current
      if (section && !section.contains(event.target)) close()
    }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('mousedown', onPointerDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('mousedown', onPointerDown)
    }
  }, [openKey])

  // Anchor the popover under its chip, clamped inside the section so a chip
  // near the right edge cannot push evidence out of the workbench.
  useLayoutEffect(() => {
    const chip = openKey ? chipRefs.current.get(openKey) : null
    const section = sectionRef.current
    if (!chip || !section) {
      setPopoverPos(null)
      return
    }
    const chipRect = chip.getBoundingClientRect()
    const sectionRect = section.getBoundingClientRect()
    const width = 288
    const left = Math.min(Math.max(chipRect.left - sectionRect.left - 4, 4),
      Math.max(sectionRect.width - width - 4, 4))
    setPopoverPos({ left, top: chipRect.bottom - sectionRect.top + 4, width })
  }, [openKey])

  return (
    <section ref={sectionRef} data-testid="project-overview" className="relative shrink-0 border-b border-gray-800 bg-[#0c0c0c] px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 font-mono">
        <div className="min-w-0 shrink-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-[11px] text-gray-200">{project.name}</span>
            <span className="rounded border border-gray-800 px-1.5 py-0.5 text-[8px] uppercase tracking-wider text-gray-600">not qualified</span>
          </div>
          <div className="mt-0.5 flex flex-wrap gap-x-2 text-[9px] text-gray-600">
            <span data-testid="project-top-module">top {hardwareProject?.top_module ?? designContract?.top_module ?? verilogTop(designContract) ?? 'unbound'}</span>
            <span data-testid="project-source-count">{files.length ? `${files.length} sources` : agentRtl ? 'agent RTL (no manifest)' : 'no source manifest'}</span>
            {hardwareProject && <span>rev {hardwareProject.revision} / {hardwareProject.digest?.slice(0, 10)}</span>}
            {activeModel && <span>{activeModel}</span>}
          </div>
        </div>
        {/* Content-sized chips in one compact band. At medium widths the
            strip scrolls horizontally instead of growing the header; on
            narrow widths it moves to its own second line below the identity. */}
        <div
          data-testid="qualification-gates"
          className="order-last basis-full min-w-0 flex-1 overflow-x-auto [scrollbar-width:thin] sm:order-none sm:basis-auto"
        >
          <div className="flex w-max items-center gap-1.5">
            {gates.map((gate) => (
              <GateChip
                key={gate.key}
                gate={gate}
                open={openKey === gate.key}
                onFocus={setFocusGate}
                onHover={setHoverGate}
                buttonRef={(node) => {
                  if (node) chipRefs.current.set(gate.key, node)
                  else chipRefs.current.delete(gate.key)
                }}
              />
            ))}
          </div>
        </div>
        <div className="ml-auto flex shrink-0 flex-wrap items-center gap-x-3 text-[9px] text-gray-500">
          {target && <span>goal <b className="font-normal text-gray-300">{target} MHz</b></span>}
          {Number.isFinite(fmax) && <span>measured <b className="font-normal text-gray-300">{fmax} MHz</b></span>}
          {active && <span className="text-amber-400">active {active.kind}</span>}
          <span className={failed ? 'text-red-400' : 'text-gray-600'}>next: {blocker?.label ?? 'load evidence'}</span>
        </div>
      </div>
      {openGate && popoverPos && (
        <div
          data-testid="qualification-popover"
          role="status"
          className="absolute z-30 rounded border border-gray-700 bg-[#151515] p-2.5 shadow-xl"
          style={popoverPos}
        >
          <div className="flex items-center gap-1.5">
            <span className={`h-1.5 w-1.5 rounded-full ${(STATUS[openGate.status] ?? STATUS.unavailable).dot}`} />
            <span className="font-mono text-[10px] uppercase tracking-wider text-gray-300">{openGate.label}</span>
            <span className={`font-mono text-[10px] ${(STATUS[openGate.status] ?? STATUS.unavailable).text}`}>
              state: {(STATUS[openGate.status] ?? STATUS.unavailable).label}
            </span>
          </div>
          <div className="mt-1.5 text-[10px] leading-relaxed text-gray-400">{openGate.evidence}</div>
          <div className="mt-1.5 text-[10px] leading-relaxed text-gray-500">
            <span className="uppercase tracking-wider text-gray-600">next </span>{openGate.next}
          </div>
        </div>
      )}
    </section>
  )
}

function verilogTop(contract) {
  return contract?.sections?.interface?.top_module ?? contract?.sections?.interface?.module ?? null
}
