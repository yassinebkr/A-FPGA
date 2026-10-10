import { Component, lazy, Suspense, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Maximize2, Minimize2 } from 'lucide-react'
import VerilogView from './VerilogView'
import JobDetails from './JobDetails'

// Keep graph rendering and the post-P&R canvas out of the initial workbench
// chunk. The dynamic modules are requested when the Visualize surface opens.
const GateSchematic = lazy(() => import('./GateSchematic'))
const PhysicalLayout = lazy(() => import('./PhysicalLayout'))

function VisualizationLoading({ label }) {
  return <div className="flex h-full items-center justify-center font-mono text-[10px] uppercase tracking-wider text-gray-600">{label}</div>
}

class VisualizationChunkBoundary extends Component {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div data-testid="visualization-load-error" className="flex h-full flex-col items-center justify-center gap-2 font-mono text-[10px] text-red-300">
        <span>visualization chunk failed to load</span>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="rounded border border-red-500/40 px-2 py-1 text-[9px] uppercase tracking-wider text-red-300 hover:bg-red-500/10"
        >
          retry
        </button>
      </div>
    )
  }
}

// Compact PPA numbers: kill float noise (0.28200000000000003) and group large
// values. Non-numeric values pass through untouched.
function formatMetric(value) {
  if (value === null || value === undefined || value === '') return value
  const n = Number(value)
  if (!Number.isFinite(n)) return value
  if (Math.abs(n) >= 100) {
    return n.toLocaleString('en-US', { maximumFractionDigits: 1 })
  }
  return String(parseFloat(n.toFixed(3))) // ≤3 decimals, trailing zeros stripped
}

function MetricCard({ title, value, unit, detail, timing = false, assurance }) {
  const display = formatMetric(value)
  const missing = display === null || display === undefined || display === ''
  const fullTitle = `${missing ? 'unavailable' : `${display}${unit ? ` ${unit}` : ''}`} — ${detail}${assurance ? ` (${assurance})` : ''}`
  return (
    <div
      className={`metric-card bg-[#111111] rounded-md px-2.5 py-1 border border-gray-800 min-w-0 h-[46px] flex flex-col justify-center ${timing ? 'metric-card-timing' : ''}`}
      title={missing ? (fullTitle || 'This metric was not produced by the selected run.') : fullTitle}
      data-testid={`metric-${title.toLowerCase().replace(/\s+/g, '-')}`}
    >
      <div className="flex items-center gap-1.5 text-gray-500 text-[9px] font-mono uppercase tracking-wider mb-0.5 min-w-0">
        <span className="truncate">{title}</span>
        {/* Assurance stays visible as a compact marker; the full word lives in
            the card tooltip so it can never force the card height. */}
        {assurance && <span
          className={`h-1 w-1 shrink-0 rounded-full ${
            assurance === 'unavailable' ? 'bg-gray-700'
              : assurance === 'estimated' ? 'bg-amber-400/70'
                : 'bg-cyan-400/70'}`}
          title={assurance}
        />}
      </div>
      <div className="text-base font-mono text-gray-100 leading-none whitespace-nowrap truncate">
        {missing ? <span className="text-gray-700">—</span> : display}
        {!missing && unit && <span className="text-xs text-gray-500 ml-1">{unit}</span>}
      </div>
    </div>
  )
}

function VisualizationPanel({ selectedJob, onOpenSource, fullscreen = false, onToggleFullscreen, visible = true }) {
  // The Visualize surface hosts the bounded gate-level schematic (Phase G2)
  // and its post-P&R companion view (Phase G4). Selection and timing-path
  // highlight are shared between both views so an instance selected in one
  // is located in the other; the layout tab only appears when the job has a
  // digest-bound layout artifact.
  const [subview, setSubview] = useState('schematic')
  const [schematicSelection, setSchematicSelection] = useState(null)
  const [pathCells, setPathCells] = useState(null)
  const [externalSelection, setExternalSelection] = useState(null)
  const [layoutInfo, setLayoutInfo] = useState(null)
  const timing = selectedJob?.manifest?.timing_diagnosis
  const layout = selectedJob?.manifest?.layout_artifacts
  // Escape leaves the fullscreen overlay; the toggle button mirrors it.
  useEffect(() => {
    if (!fullscreen) return undefined
    const onKeyDown = (event) => {
      if (event.key === 'Escape') onToggleFullscreen?.()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [fullscreen, onToggleFullscreen])

  return (
    <div
      className={fullscreen
        ? 'fixed inset-0 z-50 flex flex-col bg-[#0a0a0a] font-mono'
        : 'flex h-full min-h-0 flex-col font-mono'}
      data-testid="visualization-foundation"
    >
      <div className="flex shrink-0 items-center gap-1 border-b border-gray-800 bg-[#0d0d0d] px-3 py-1">
        {['schematic', 'layout'].map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => setSubview(key)}
            data-testid={`visualize-subview-${key}`}
            className={`rounded px-2 py-0.5 text-[9px] uppercase tracking-wider transition-colors ${
              subview === key ? 'border border-gray-600 bg-white/5 text-gray-200' : 'border border-transparent text-gray-600 hover:text-gray-400'}`}
          >
            {key}
            {key === 'layout' && layoutInfo === null ? ' (checking…)' : ''}
            {key === 'layout' && layoutInfo && layoutInfo.available === false ? ' (unavailable)' : ''}
          </button>
        ))}
        {visible && (
          <span className="ml-2 hidden truncate text-[9px] text-gray-600 sm:inline" title="Evidence source of the visualization">
            {selectedJob ? `${selectedJob.kind} ${selectedJob.id} · ${selectedJob.design}` : ''}
          </span>
        )}
        <button
          type="button"
          onClick={() => onToggleFullscreen?.()}
          data-testid="visualize-fullscreen-toggle"
          className="ml-auto flex items-center gap-1 rounded border border-gray-800 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-gray-500 hover:border-gray-600 hover:text-gray-300"
          title={fullscreen ? 'Exit fullscreen (Escape)' : 'Open the visualization in fullscreen'}
          aria-label={fullscreen ? 'Exit fullscreen visualization' : 'Enter fullscreen visualization'}
        >
          {fullscreen ? <Minimize2 className="h-3 w-3" /> : <Maximize2 className="h-3 w-3" />}
          {fullscreen ? 'exit fullscreen' : 'fullscreen'}
        </button>
      </div>
      <div className="relative min-h-0 flex-1">
        <div className={subview === 'schematic' ? 'absolute inset-0' : 'hidden'}>
          <VisualizationChunkBoundary>
            <Suspense fallback={<VisualizationLoading label="loading gate schematic…" />}>
              <GateSchematic
                job={selectedJob}
                onOpenSource={onOpenSource}
                onSelectionChange={setSchematicSelection}
                onPathChange={setPathCells}
                externalSelection={externalSelection}
              />
            </Suspense>
          </VisualizationChunkBoundary>
        </div>
        <div className={subview === 'layout' ? 'absolute inset-0' : 'hidden'}>
          <VisualizationChunkBoundary>
            <Suspense fallback={<VisualizationLoading label="loading physical layout…" />}>
              <PhysicalLayout
                job={selectedJob}
                selection={schematicSelection}
                pathCells={pathCells}
                onSelectComponent={(id) => setExternalSelection({ id, nonce: Date.now() })}
                onAvailability={setLayoutInfo}
              />
            </Suspense>
          </VisualizationChunkBoundary>
        </div>
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-0.5 border-t border-gray-800 bg-[#0d0d0d] px-3 py-1.5 text-[9px] text-gray-600">
        {timing?.paths?.length
          ? <span data-testid="visualization-timing-strip">timing paths: {timing.paths.length} reported · worst slack {timing.setup_slack_ns ?? timing.paths[0]?.slack_ns ?? '?'} ns</span>
          : <span>timing paths: unavailable — select a job with a digest-bound timing report</span>}
        {layout?.artifacts?.length
          ? <span>physical evidence: {layout.artifacts.length} hash-verified artifact(s)</span>
          : <span>physical evidence: unavailable — no hash-verified post-P&amp;R artifact for the selected job</span>}
      </div>
    </div>
  )
}

function MetricProvenance({ metrics, provenance = {} }) {
  const meta = { ...(metrics?.provenance ?? metrics?.metadata ?? {}), ...provenance }
  const run = metrics?.run_id ?? metrics?.runId ?? meta.run_id ?? meta.runId
  const candidate = metrics?.candidate ?? metrics?.candidate_id ?? meta.candidate ?? meta.candidate_id
  const stage = metrics?.evidence_stage ?? metrics?.stage ?? meta.evidence_stage ?? meta.stage
  const comparison = metrics?.comparison ?? meta.comparison
  if (!run && !candidate && !stage && !comparison) return null
  return (
    <div className="px-3 py-1.5 border-b border-gray-800 bg-[#0f0f0f] flex items-center gap-x-3 gap-y-1 flex-wrap text-[9px] font-mono text-gray-500" data-testid="metric-provenance">
      <span className="uppercase tracking-wider text-gray-600">selected</span>
      {run && <span title={String(run)}>run <b className="text-gray-300 font-normal">{String(run).slice(0, 16)}</b></span>}
      {candidate && <span title={String(candidate)}>candidate <b className="text-gray-300 font-normal">{String(candidate).slice(0, 16)}</b></span>}
      {stage && <span>stage <b className="text-gray-300 font-normal">{stage}</b></span>}
      {comparison && <details className="ml-auto relative"><summary className="cursor-pointer text-gray-400 hover:text-gray-200">compare</summary><div className="absolute z-10 right-0 top-5 w-56 rounded border border-gray-700 bg-[#151515] p-2 text-gray-400 shadow-xl">{typeof comparison === 'string' ? comparison : `Compared with ${comparison.label ?? comparison.run_id ?? 'compatible result'}`}</div></details>}
    </div>
  )
}

const JOB_STATUS_CHIP = {
  queued: 'bg-blue-500/10 text-blue-400 border-blue-500/40',
  running: 'bg-amber-500/10 text-amber-400 border-amber-500/40 animate-pulse',
  done: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/40',
  failed: 'bg-red-500/10 text-red-400 border-red-500/40',
  cancelled: 'bg-gray-500/10 text-gray-400 border-gray-500/40',
  interrupted: 'bg-gray-500/10 text-gray-400 border-gray-500/40',
}

// Discreet per-job timing: start clock time, plus the measured runtime once
// the job is finished (elapsed-agnostic while it runs — no ticking timer to
// avoid re-render churn in the job strip).
function jobTiming(job) {
  const startMs = job.startedAt ?? Date.parse(job.created_at ?? '')
  if (!Number.isFinite(startMs)) return null
  const started = new Date(startMs).toLocaleTimeString([], { hour12: false })
  const endMs = Date.parse(job.finished_at ?? '')
  if (Number.isFinite(endMs)) {
    const seconds = Math.max(0, (endMs - startMs) / 1000)
    const runtime = seconds >= 60
      ? `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
      : `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`
    return `${started} · ${runtime}`
  }
  return started
}

function JobConsole({ job, onCancel, collapsed, onToggle }) {
  const logRef = useRef(null)

  const followRef = useRef(true)

  // Follow live output until the user scrolls away; scrolling back to the
  // bottom resumes follow without remounting the console.
  useEffect(() => {
    const el = logRef.current
    if (el && followRef.current) el.scrollTop = el.scrollHeight
  }, [job.lines.length])

  const handleScroll = () => {
    const el = logRef.current
    if (!el) return
    followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
  }

  const displayStatus = job.status === 'done' && job.outcome === 'failure'
    ? 'failed'
    : job.status === 'done' && job.outcome === 'inconclusive'
      ? 'inconclusive'
      : job.status
  const chip = JOB_STATUS_CHIP[displayStatus] ??
    (displayStatus === 'inconclusive'
      ? 'bg-orange-500/10 text-orange-400 border-orange-500/40'
      : JOB_STATUS_CHIP.queued)
  const live = job.status === 'queued' || job.status === 'running'
  const header = (
    <>
      <span className={`px-1.5 py-0.5 rounded border text-[9px] font-mono uppercase tracking-wider ${chip}`}>
        {displayStatus}
      </span>
      <span className="text-[10px] font-mono text-gray-400">{job.kind ?? 'job'}</span>
      <span className="text-[10px] font-mono text-gray-500 truncate">{job.design}</span>
      <span className="text-[9px] font-mono text-gray-700 shrink-0" title="start time and runtime">{jobTiming(job)}</span>
      <span className="text-[10px] font-mono text-gray-700 ml-auto shrink-0">{job.id}</span>
    </>
  )

  // Finished jobs collapse to a one-line row; expanding replays the stored log.
  if (collapsed) {
    return (
      <button
        onClick={onToggle}
        className="flex items-center gap-2 w-full text-left border border-gray-800 rounded-md bg-[#0a0a0a] px-2.5 py-1.5 hover:border-gray-600 transition-colors shrink-0"
      >
        <ChevronRight className="w-3 h-3 text-gray-600 shrink-0" />
        {header}
      </button>
    )
  }

  return (
    <div className="border border-gray-800 rounded-md bg-[#0a0a0a] overflow-hidden shrink-0">
      <div
        className={`flex items-center gap-2 px-2.5 py-1.5 bg-[#111111] border-b border-gray-800 ${live ? '' : 'cursor-pointer hover:border-gray-600'}`}
        onClick={live ? undefined : onToggle}
        title={live ? undefined : 'Collapse'}
      >
        {!live && <ChevronDown className="w-3 h-3 text-gray-600 shrink-0" />}
        {header}
        {live && (
          <button
            onClick={() => onCancel(job.id)}
            className="px-1.5 py-0.5 rounded border border-red-500/40 text-red-400 text-[9px] font-mono uppercase tracking-wider hover:bg-red-500/10 transition-colors shrink-0"
            title="Cancel job (kills the process tree)"
          >
            kill
          </button>
        )}
      </div>
      <div
        ref={logRef}
        onScroll={handleScroll}
        data-testid={`job-log-${job.id}`}
        className="max-h-44 overflow-y-auto px-2.5 py-1.5 font-mono text-[10px] leading-relaxed text-gray-400 whitespace-pre-wrap break-all"
      >
        {job.lines.length === 0 ? (
          <div className="text-gray-700">waiting for output…</div>
        ) : (
          job.lines.map((line, i) => <div key={job.lineSeqs?.[i] ?? `${job.id}:${i}`}>{line}</div>)
        )}
      </div>
    </div>
  )
}

export default function LiveStateDashboard({ metrics, metricProvenance = {}, verilog, hardwareProject, activity, jobs, jobsState = 'loading', onCancelJob, onSubscribeJob, projectId }) {
  // Jobs are a GLOBAL registry; by default only the current project's jobs
  // (tagged with its session id at submit time) are shown. "show all" reveals
  // the global view (including pre-tagging jobs, which carry session: null).
  const [showAll, setShowAll] = useState(false)
  // Scope is project-local UI state. Do not carry an "all jobs" choice into
  // another project when the dashboard component is reused by React.
  useEffect(() => {
    // Deliberate project-boundary reset for a reused dashboard.
    // oxlint-disable-next-line react/set-state-in-effect
    setShowAll(false)
  }, [projectId])
  const allJobs = Object.values(jobs ?? {})
  const visible = projectId && !showAll
    ? allJobs.filter((j) => j.session === projectId)
    : allJobs
  // Live jobs first (running before queued), finished ones newest-first.
  const STATUS_RANK = { running: 0, queued: 1 }
  const jobStart = (j) => j.startedAt ?? Date.parse(j.created_at ?? 0) ?? 0
  const jobList = [...visible].sort((a, b) =>
    (STATUS_RANK[a.status] ?? 2) - (STATUS_RANK[b.status] ?? 2) ||
    jobStart(b) - jobStart(a) ||
    String(a.id).localeCompare(String(b.id)))
  const hasLiveJob = jobList.some((j) => j.status === 'queued' || j.status === 'running')
  const isLive = (j) => j.status === 'queued' || j.status === 'running'

  // Finished jobs are collapsed by default; expanding one replays its stored
  // log (the server replays from the ring buffer, or the disk log after a
  // server restart).
  const [expandedJobs, setExpandedJobs] = useState(new Set())
  const [selectedJobId, setSelectedJobId] = useState(null)
  const toggleJob = (job) => {
    const expanding = !expandedJobs.has(job.id)
    setExpandedJobs((prev) => {
      const next = new Set(prev)
      if (expanding) next.add(job.id); else next.delete(job.id)
      return next
    })
    if (expanding && job.lines.length === 0) onSubscribeJob?.(job.id, { replay: true })
  }

  // Preserve the user's selected evidence surface. A new job appears in the
  // active strip without forcibly replacing the source/result being viewed.
  const [tab, setTab] = useState('code')
  // Keep the visualization mounted after its first visit so selection and
  // viewport state survive tab switches, while Sources/Jobs users avoid
  // downloading the graph/layout chunks entirely.
  const [vizVisited, setVizVisited] = useState(false)
  const [vizFullscreen, setVizFullscreen] = useState(false)
  // Source cross-probing target: set by the schematic (Phase G2) and consumed
  // by the Sources surface, which selects and scrolls to the referenced file
  // and line. A nonce makes selecting the same source twice re-focus.
  const [sourceFocus, setSourceFocus] = useState(null)
  const openSource = (focus) => {
    if (!focus?.file) return
    setSourceFocus({ ...focus, nonce: Date.now() })
    setTab('code')
  }
  const selectedJob = jobList.find((job) => job.id === selectedJobId) ?? jobList[0] ?? null
  // Only synthesis/fmax jobs produce a gate netlist; when the default or the
  // user's selection is another kind (e.g. a failed simulation), the
  // visualization falls back to the newest implementation job so the
  // schematic shows the real evidence instead of an empty panel.
  const vizJob = (selectedJob && ['synthesis', 'fmax'].includes(selectedJob.kind)
    ? selectedJob
    : jobList.find((job) => ['synthesis', 'fmax'].includes(job.kind))) ?? selectedJob

  return (
    <div className="bg-[#0d0d0d] rounded-lg border border-gray-800 flex flex-col h-full overflow-hidden">
      <div className="px-4 py-2.5 border-b border-gray-800 flex items-center justify-between bg-[#111111]">
        <h2 className="text-xs font-mono uppercase tracking-wider text-gray-400">Synthesis Workbench</h2>
        <span className="text-[10px] text-gray-600 font-mono">project evidence</span>
      </div>

      <MetricProvenance metrics={metrics} provenance={metricProvenance} />
      <div className="metric-grid p-3 border-b border-gray-800">
        <div className="metric-grid-layout gap-2">
        <MetricCard title="Cell Area" value={metrics.cellarea} unit="um²" assurance={metrics.cellarea == null ? 'unavailable' : 'screening'} detail="Standard-cell area from the selected result." />
        <MetricCard title="Cells" value={metrics.cells} assurance={metrics.cells == null ? 'unavailable' : 'screening'} detail="Count of synthesized cells." />
        <MetricCard title="Utilization" value={metrics.utilization} unit="%" assurance={metrics.utilization == null ? 'unavailable' : 'screening'} detail={metrics.utilization == null ? 'Requires place & route — a synthesis screen has no die area.' : 'Placed cell area over die area.'} />
        <MetricCard title="Estimated power" value={metrics.power} unit="mW" assurance={metrics.power == null ? 'unavailable' : 'estimated'} detail="Tool-estimated power; not signoff power." />
        <MetricCard title="Setup slack" value={metrics.setupslack} unit="ns" timing assurance={metrics.setupslack == null ? 'unavailable' : 'screening'} detail="Worst setup slack from timing analysis." />
        <MetricCard title="Hold slack" value={metrics.holdslack} unit="ns" timing assurance={metrics.holdslack == null ? 'unavailable' : 'screening'} detail="Worst hold slack (fast corner, min-delay paths). 0 ns is a real measured value, not a placeholder." />
        <MetricCard title="Fmax" value={metrics.fmax} unit="MHz" timing assurance={metrics.fmax == null ? 'unavailable' : 'screening'} detail="Screening frequency from the selected evidence stage." />
        <MetricCard title="Critical delay" value={metrics.criticalpath ?? metrics.critical_path_delay ?? metrics.delay} unit="ns" timing assurance={metrics.criticalpath_derived ? 'derived' : (metrics.criticalpath ?? metrics.critical_path_delay ?? metrics.delay) == null ? 'unavailable' : 'screening'} detail={metrics.criticalpath_derived ? 'Derived: clock period − worst setup slack (single-clock identity).' : 'Critical-path delay when explicitly reported.'} />
        </div>
      </div>
      {(metrics.target_mhz ?? metrics.targetMHz ?? metrics.evidence_stage ?? metrics.stage ?? metricProvenance.evidence_stage ?? metricProvenance.stage) && (
        <div className="px-3 py-1 border-b border-gray-800 text-[9px] font-mono text-gray-600">
          target {formatMetric(metrics.target_mhz ?? metrics.targetMHz ?? 50)} MHz · {metrics.fmax == null ? 'measurement pending' : Number(metrics.fmax) >= Number(metrics.target_mhz ?? metrics.targetMHz ?? 50) ? 'meets target' : `shortfall ${(Number(metrics.target_mhz ?? metrics.targetMHz ?? 50) - Number(metrics.fmax)).toFixed(2)} MHz`}{metrics.evidence_stage ?? metrics.stage ?? metricProvenance.evidence_stage ?? metricProvenance.stage ? ` · ${metrics.evidence_stage ?? metrics.stage ?? metricProvenance.evidence_stage ?? metricProvenance.stage}` : ''}
        </div>
      )}

      {hasLiveJob && <button onClick={() => setTab('jobs')} className="mx-3 mt-2 rounded border border-amber-900/50 bg-amber-950/10 px-2.5 py-1.5 flex items-center gap-2 text-left" data-testid="active-job-strip"><span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse"/><span className="text-[10px] font-mono text-amber-300">{jobList.filter(isLive).length} active job{jobList.filter(isLive).length === 1 ? '' : 's'}</span><span className="ml-auto text-[9px] font-mono text-gray-600">open jobs</span></button>}
      <div className="flex items-center gap-1 px-3 pt-2 border-b border-gray-800">
        {[['code', 'Sources'], ['jobs', `Jobs${jobList.length ? ` (${jobList.length})` : ''}`], ['results', 'Results'], ['visualize', 'Visualize']].map(([key, label]) => (
          <button
            key={key}
            onClick={() => { setTab(key); if (key === 'visualize') setVizVisited(true) }}
            className={`px-3 py-1.5 text-[10px] font-mono uppercase tracking-wider rounded-t-md border border-b-0 transition-colors ${
              tab === key
                ? 'bg-[#0a0a0a] border-gray-800 text-gray-200'
                : 'border-transparent text-gray-600 hover:text-gray-400'
            }`}
          >
            {label}
          </button>
        ))}
        {tab === 'jobs' && hasLiveJob && (
          <span className="ml-auto w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse mb-1.5" title="job running" />
        )}
        {projectId && (
          <button
            onClick={() => setShowAll((v) => !v)}
            className={`${tab === 'jobs' && hasLiveJob ? '' : 'ml-auto'} px-2 py-1 mb-0.5 text-[9px] font-mono uppercase tracking-wider rounded border transition-colors ${
              showAll ? 'border-gray-600 text-gray-300' : 'border-gray-800 text-gray-600 hover:text-gray-400'
            }`}
            title={showAll ? 'Showing the global job registry' : 'Showing only this project\'s jobs'}
            aria-pressed={showAll}
          >
            {showAll ? 'scope: all jobs' : 'scope: this project'}
          </button>
        )}
      </div>

      <div className="flex-grow min-h-0 bg-[#0a0a0a]">
        {/* The Visualize surface stays mounted across tab switches so the
            schematic's scope and selection survive (Phase G2); it is only
            hidden while another surface is active. */}
        {(vizVisited || vizFullscreen) && <div className={tab === 'visualize' || vizFullscreen ? 'h-full' : 'hidden'}>
          <VisualizationPanel
            selectedJob={vizJob}
            onOpenSource={openSource}
            fullscreen={vizFullscreen}
            visible={tab === 'visualize' || vizFullscreen}
            onToggleFullscreen={() => setVizFullscreen((v) => !v)}
          />
        </div>}
        {tab !== 'visualize' && (tab === 'code' ? (
          <VerilogView verilog={verilog} hardwareProject={hardwareProject} focusSource={sourceFocus} />
        ) : tab === 'results' ? (
          (vizJob ?? selectedJob) ? <div className="h-full overflow-y-auto"><JobDetails job={vizJob ?? selectedJob} /></div> : <div className="px-3 py-4 text-xs text-gray-600 font-mono">No result selected.</div>
        ) : jobList.length === 0 ? (
          <div className="px-3 py-4 text-xs text-gray-600 font-mono">{jobsState === 'unavailable' ? 'Job registry unavailable — reconnecting…' : jobsState === 'loading' ? 'Loading EDA jobs…' : 'No EDA jobs yet — lint runs inline; simulation or synthesis will appear here.'}</div>
        ) : (
          <div className="h-full overflow-y-auto p-3 flex flex-col gap-2">
            {jobList.map((job) => (
              <JobConsole
                key={job.id}
                job={job}
                onCancel={onCancelJob}
                collapsed={!isLive(job) && !expandedJobs.has(job.id)}
                onToggle={() => { setSelectedJobId(job.id); toggleJob(job) }}
              />
            ))}
          </div>
        ))}
      </div>

      <div className="border-t border-gray-800 bg-[#111111] max-h-36 overflow-y-auto">
        <div className="px-3 py-1.5 text-[10px] font-mono uppercase tracking-wider text-gray-500 border-b border-gray-800">
          Activity
        </div>
        {activity.length === 0 ? (
          <div className="px-3 py-2 text-xs text-gray-600 font-mono">no tool runs yet</div>
        ) : (
          activity.map((item, i) => (
            <div key={i} className="px-3 py-1.5 flex items-center gap-2 text-xs font-mono border-b border-gray-800/50 last:border-0">
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${item.ok ? 'bg-emerald-500' : 'bg-red-500'}`} />
              {item.cached && <span className="px-1 rounded border border-cyan-500/30 text-cyan-400 text-[9px]">cached</span>}
              <span className="text-gray-400 truncate">{item.summary}</span>
              <span className="text-gray-700 ml-auto shrink-0">{item.time.toLocaleTimeString()}</span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
