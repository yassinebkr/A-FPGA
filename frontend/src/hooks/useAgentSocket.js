import { useCallback, useEffect, useRef, useState } from 'react'
import { toolResultOk } from '@/lib/toolResult'
import { isArtifactHandle } from '@/lib/artifactHandle'
import { isStructuredTimelineFrame, normalizeTimelineFrame } from '@/lib/timeline'
import { applyDesignContractFrame, contractGate, isDesignContractFrame } from '@/lib/designContract'

const BACKEND_HOST = window.location.hostname || 'localhost'
const BACKEND_PORT = import.meta.env.VITE_BACKEND_PORT || '8000'
const WS_URL = `ws://${BACKEND_HOST}:${BACKEND_PORT}/ws`
const API_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}`

// Parse one tool result into a complete metric snapshot. Missing fields remain
// absent; callers replace the previous snapshot atomically to avoid mixing jobs.
export function extractMetricSnapshot(text) {
  const metrics = {}
  const patterns = {
    cellarea: /cellarea:\s*([0-9.eE+-]+)/i,
    totalarea: /totalarea:\s*([0-9.eE+-]+)/i,
    utilization: /utilization:\s*([0-9.eE+-]+)/i,
    cells: /(?:^|\n)cells:\s*(\d+)/i,
    power: /(?:^|\n)(?:peakpower|power):\s*([0-9.eE+-]+)/i,
    setupslack: /setupslack:\s*([0-9.eE+-]+)/i,
    holdslack: /holdslack:\s*([0-9.eE+-]+)/i,
    clockperiod: /clock_period_ns":\s*([0-9.eE+-]+)/,
    fmax: /(?:fmax|fmax_mhz):\s*([0-9.eE+-]+)/i,
    criticalpath: /(?:criticalpath|critical_path_delay|critical path delay):\s*([0-9.eE+-]+)/i,
    target_mhz: /target_mhz:\s*([0-9.eE+-]+)/i,
  }
  for (const [key, re] of Object.entries(patterns)) {
    const m = String(text ?? '').match(re)
    if (!m) continue
    const value = Number(m[1])
    if (!Number.isFinite(value)) continue
    metrics[key] = key === 'fmax' && value > 1000 ? value / 1e6 : value
  }
  // Structured Fmax results are authoritative and may contain zero slack.
  for (const line of String(text ?? '').split(/\r?\n/)) {
    try {
      const payload = JSON.parse(line)
      if (!payload || typeof payload !== 'object') continue
      const source = payload.metrics && typeof payload.metrics === 'object' ? payload.metrics : payload
      for (const [from, to] of [['fmax_mhz', 'fmax'], ['wns_ns', 'setupslack'], ['targetMHz', 'target_mhz']]) {
        if (source[from] !== undefined && source[from] !== null && Number.isFinite(Number(source[from]))) metrics[to] = Number(source[from])
      }
      for (const key of ['cellarea', 'totalarea', 'utilization', 'cells', 'power', 'peakpower', 'setupslack', 'holdslack', 'fmax', 'target_mhz']) {
        if (source[key] !== undefined && source[key] !== null && Number.isFinite(Number(source[key]))) metrics[key] = Number(source[key])
      }
      if (metrics.fmax > 1000) metrics.fmax /= 1e6
    } catch { /* legacy key/value result */ }
  }
  // The critical-path delay is not printed as its own metric by the
  // synthesis+STA screen, but it is exactly derived for a single-clock
  // constrained design as period − worst setup slack. Flag the derivation so
  // the card can label it honestly instead of passing it as a measured value.
  if (metrics.criticalpath === undefined && metrics.clockperiod !== undefined &&
      metrics.setupslack !== undefined && Number.isFinite(metrics.clockperiod) &&
      Number.isFinite(metrics.setupslack)) {
    metrics.criticalpath = Number((metrics.clockperiod - metrics.setupslack).toFixed(3))
    metrics.criticalpath_derived = true
  }
  delete metrics.clockperiod
  return metrics
}

export function extractMetricProvenance(text) {
  const provenance = {}
  for (const line of String(text ?? '').split(/\r?\n/)) {
    const job = line.match(/^job_id:\s*(\S+)/i)
    if (job) provenance.job_id = job[1]
    try {
      const payload = JSON.parse(line)
      if (!payload || typeof payload !== 'object') continue
      for (const key of ['job_id', 'run_id', 'candidate_id', 'evidence_stage', 'stage', 'fmax_source']) {
        if (payload[key] !== undefined && payload[key] !== null) provenance[key] = payload[key]
      }
      if (payload.fmax_search || payload.search) {
        provenance.fmax_provenance = payload.fmax_search || payload.search
        provenance.evidence_stage ||= 'fmax_search'
      }
    } catch { /* legacy result */ }
  }
  if (Object.keys(provenance).length && !provenance.evidence_stage) provenance.evidence_stage = 'screening_sta'
  return provenance
}

export function extractMetrics(text, prev = {}) {
  const snapshot = extractMetricSnapshot(text)
  return Object.keys(snapshot).length ? snapshot : prev
}

const JOB_STATUS_RANK = {
  queued: 0,
  running: 1,
  done: 2,
  failed: 2,
  cancelled: 2,
  interrupted: 2,
}

function mergeHydratedStatus(current, incoming) {
  if (!incoming) return current
  if (!current) return incoming
  // REST is a point-in-time snapshot and can arrive after newer live frames.
  // Never rewind a job, and never replace one terminal outcome with another
  // terminal snapshot from the registry.
  const currentRank = JOB_STATUS_RANK[current] ?? -1
  const incomingRank = JOB_STATUS_RANK[incoming] ?? -1
  return incomingRank > currentRank ? incoming : current
}

function mergeLiveStatus(current, incoming) {
  if (!incoming) return current
  if (!current) return incoming
  const currentRank = JOB_STATUS_RANK[current] ?? -1
  const incomingRank = JOB_STATUS_RANK[incoming] ?? -1
  return incomingRank >= currentRank ? incoming : current
}

function isLiveStatus(status) {
  return status === "queued" || status === "running" || status === "done" ||
    status === "failed" || status === "cancelled" || status === "interrupted"
}

const GREETING = { role: 'agent', content: 'Ready. Tell me what hardware you want to build.' }

const EMPTY_REASONING = {
  active: false,
  content: '',
  round: null,
  generationId: null,
  usage: null,
}

const TERMINAL_RUN_STATES = new Set([
  'completed', 'complete', 'done', 'failed', 'failure', 'cancelled',
  'canceled', 'interrupted', 'inconclusive', 'blocked',
])

function runStateValue(frame) {
  if (!frame || typeof frame !== 'object') return ''
  const content = frame.content && typeof frame.content === 'object' ? frame.content : frame
  return String(content.state ?? content.status ?? content.lifecycle ?? content.outcome ?? '').toLowerCase()
}

function isTerminalRunFrame(frame) {
  return TERMINAL_RUN_STATES.has(runStateValue(frame))
}

// Per-job log-cursor persistence so a reconnecting client can resume a
// running job's live log exactly where it left off (subscribe since_seq).
const JOB_SEQ_KEY = (jobId) => `agent:joblastseq:${jobId}`
const TIMELINE_KEY = (projectId) => projectId ? `agent:timeline:${projectId}` : null
const storedSeq = (jobId) => parseInt(localStorage.getItem(JOB_SEQ_KEY(jobId)) || '0', 10) || 0

export function useAgentSocket(projectId) {
  const [status, setStatus] = useState('connecting') // connecting | open | closed
  const [model, setModel] = useState(null)
  const [messages, setMessages] = useState([GREETING])
  const [busy, setBusy] = useState(false)
  const [isRunning, setIsRunning] = useState(false)
  const [statusText, setStatusText] = useState(null) // latest "status" frame (LLM-wait heartbeat)
  const [runState, setRunState] = useState(null) // authoritative durable lifecycle snapshot
  const [generation, setGeneration] = useState({ active: false, round: null, generationId: null, usage: null })
  const [reasoning, setReasoning] = useState(EMPTY_REASONING)
  const [timeline, setTimeline] = useState([]) // concise structured progress/decision events
  const [metrics, setMetrics] = useState({})
  const [metricProvenance, setMetricProvenance] = useState({})
  const [verilog, setVerilog] = useState(null) // { top, code }
  const [latestLint, setLatestLint] = useState(null) // { verdict, ok, code_sha256, bound }
  const [hardwareProject, setHardwareProject] = useState(null)
  const [designContract, setDesignContract] = useState(null)
  const [contractActionPending, setContractActionPending] = useState(false)
  const [activity, setActivity] = useState([]) // [{ ok, summary, time }]
  const [jobs, setJobs] = useState({}) // jobId -> { id, kind, design, status, lines, lastSeq, startedAt }
  // The Jobs tab must distinguish an empty registry from a failed registry.
  // Lint is intentionally inline and therefore creates no job.
  const [jobsState, setJobsState] = useState('loading') // loading | ready | unavailable
  const ws = useRef(null)
  const jobsRef = useRef({}) // mirror of `jobs` for use inside socket callbacks
  // A reconnect can run both the in-memory reattach pass and REST hydration.
  // Keep one subscription per socket/job so logs are not delivered twice.
  const subscribedJobsRef = useRef({ socket: null, ids: new Set() })
  const pendingJobCancelsRef = useRef(new Set())
  const timelineRef = useRef([])
  const reasoningRef = useRef(EMPTY_REASONING)
  const generationRef = useRef({ active: false, round: null, generationId: null, streamId: null, usage: null })
  const designContractRef = useRef(null)

  const applyRunState = useCallback((frame) => {
    if (!frame || typeof frame !== 'object') return
    const content = frame.content && typeof frame.content === 'object' ? frame.content : frame
    const next = { ...content }
    delete next.type
    setRunState(next)
    const terminal = isTerminalRunFrame(next)
    setBusy(!terminal)
    setIsRunning(!terminal)
    setStatusText(next.terminal_reason || next.reason || next.stop_reason || next.message || null)
  }, [])

  const applyContract = useCallback((frame) => {
    const next = applyDesignContractFrame(designContractRef.current, frame)
    designContractRef.current = next
    setDesignContract(next)
    // The approval acknowledgement precedes the resumed model stream. Keep
    // the composer closed across that small hand-off window.
    if (next?.status === 'locked' && next?.pendingRequest) {
      setBusy(true)
      setIsRunning(true)
    }
    // Contract state is the acknowledgement. Never optimistically promote the
    // UI; clear the local transport guard only after an authoritative frame.
    setContractActionPending(false)
  }, [])

  const updateReasoning = useCallback((patch) => {
    const next = { ...reasoningRef.current, ...patch }
    reasoningRef.current = next
    setReasoning(next)
  }, [])

  const resetGeneration = useCallback(() => {
    generationRef.current = { active: false, round: null, generationId: null, streamId: null, usage: null }
    setGeneration({ active: false, round: null, generationId: null, usage: null })
  }, [])

  const startGeneration = useCallback((frame = {}) => {
    const generationId = frame.generation_id ?? frame.generationId ?? null
    const nextRound = frame.round ?? null
    const previousReasoning = reasoningRef.current
    const previousContent = previousReasoning.content || ''
    const roundChanged = previousContent && previousReasoning.round !== null &&
      nextRound !== null && previousReasoning.round !== nextRound
    const next = {
      active: true,
      round: nextRound,
      generationId,
      streamId: null,
      usage: null,
    }
    generationRef.current = next
    setGeneration({ active: true, round: next.round, generationId, usage: null })
    updateReasoning({
      active: true,
      // One user run may contain several provider generations (tool rounds or
      // a bounded output-limit continuation). Keep the complete exposed
      // reasoning for the run instead of erasing earlier generations.
      content: roundChanged
        ? `${previousContent}\n\n--- round ${nextRound} ---\n\n`
        : previousContent,
      round: next.round,
      generationId,
      usage: null,
        })
  }, [updateReasoning])

  const appendReasoning = useCallback((delta, frame = {}) => {
    const text = String(delta ?? '')
    if (!text) return
    if (!generationRef.current.active) startGeneration(frame)
    const current = reasoningRef.current.content || ''
    updateReasoning({
      active: true,
      // The Working tile is folded by default, so retain the complete
      // provider stream for this run rather than silently dropping its
      // beginning after an arbitrary character limit.
      content: current + text,
      round: frame.round ?? reasoningRef.current.round,
      generationId: frame.generation_id ?? frame.generationId ?? reasoningRef.current.generationId,
    })
  }, [startGeneration, updateReasoning])

  const appendTextDelta = useCallback((delta, frame = {}) => {
    const text = String(delta ?? '')
    if (!text) return
    if (!generationRef.current.active) startGeneration(frame)
    const incomingGenerationId = frame.generation_id ?? frame.generationId ?? null
    if (incomingGenerationId && generationRef.current.generationId && incomingGenerationId !== generationRef.current.generationId) {
      startGeneration(frame)
    }
    let streamId = generationRef.current.streamId
    if (!streamId) {
      streamId = `stream-${Date.now()}-${Math.random().toString(36).slice(2)}`
      generationRef.current.streamId = streamId
    }
    setMessages((prev) => {
      const index = prev.findIndex((message) => message.streamId === streamId)
      if (index < 0) return [...prev, { role: 'agent', content: text, streamId, ts: frame.ts }]
      const next = [...prev]
      next[index] = { ...next[index], content: next[index].content + text }
      return next
    })
  }, [startGeneration])

  const clearTimeline = useCallback(() => {
    timelineRef.current = []
    setTimeline([])
  }, [])

  const appendTimeline = useCallback((frame) => {
    const incoming = normalizeTimelineFrame(frame)
    if (!incoming.length) return
    const current = [...timelineRef.current]
    for (const event of incoming) {
      const existing = current.findIndex((item) => item.id === event.id)
      if (existing >= 0) current[existing] = { ...current[existing], ...event }
      else current.push(event)
    }
    // A malformed or noisy producer must not grow the session UI forever.
    timelineRef.current = current.slice(-100)
    setTimeline(timelineRef.current)
    const key = TIMELINE_KEY(projectId)
    if (key) {
      try { localStorage.setItem(key, JSON.stringify(timelineRef.current)) } catch { /* storage is optional */ }
    }
  }, [projectId])

  const updateJob = useCallback((jobId, updater, live = false) => {
    const cur = jobsRef.current[jobId] ?? {
      id: jobId, kind: null, design: null, status: 'queued', lines: [],
      lineSeqs: [], lastSeq: storedSeq(jobId), startedAt: Date.now(), session: null,
      fresh: false, liveStatusVersion: 0, liveMetadataVersion: 0,
    }
    const next = { ...cur, ...updater(cur) }
    if (live) {
      next.liveMetadataVersion = Math.max(cur.liveMetadataVersion ?? 0, 1)
      if (isLiveStatus(next.status)) next.liveStatusVersion = Math.max(cur.liveStatusVersion ?? 0, 1)
    }
    jobsRef.current = { ...jobsRef.current, [jobId]: next }
    setJobs({ ...jobsRef.current })
  }, [])

  // replay: start from seq 0 (used when expanding a finished job whose log
  // was never streamed to this client — the server replays ring/disk log).
  const subscribeJob = useCallback((jobId, { replay = false } = {}) => {
    const socket = ws.current
    if (!socket || socket.readyState !== WebSocket.OPEN) return
    if (subscribedJobsRef.current.socket !== socket) {
      subscribedJobsRef.current = { socket, ids: new Set() }
    }
    // Explicit replay (finished-job expansion) intentionally bypasses the
    // live subscription dedupe.
    if (!replay && subscribedJobsRef.current.ids.has(jobId)) return
    if (replay) {
      // A full replay deliberately starts a new local log view. Otherwise a
      // cursor restored from localStorage would discard every replayed line.
      try { localStorage.removeItem(JOB_SEQ_KEY(jobId)) } catch { /* optional */ }
      updateJob(jobId, (job) => ({ ...job, lines: [], lineSeqs: [], lastSeq: 0 }))
    }
    subscribedJobsRef.current.ids.add(jobId)
    socket.send(JSON.stringify({
      type: 'subscribe', job_id: jobId, since_seq: replay ? 0 : storedSeq(jobId),
    }))
  }, [updateJob])

  const cancelJob = useCallback((jobId) => {
    if (!jobId) return
    if (!ws.current || ws.current.readyState !== WebSocket.OPEN) {
      // Preserve the user's cancellation across a reconnect. The backend owns
      // the process; this only queues the control frame until transport returns.
      pendingJobCancelsRef.current.add(jobId)
      return
    }
    ws.current.send(JSON.stringify({ type: 'cancel_job', job_id: jobId }))
  }, [])

  useEffect(() => {
    let cancelled = false
    let socket
    let retryTimer
    // Job registry hydration is network state scoped to the current project.
    // oxlint-disable-next-line react/set-state-in-effect
    setJobsState('loading')

    fetch(`${API_URL}/health`)
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => { if (!cancelled && body?.model) setModel(body.model) })
      .catch(() => {})

    const connect = () => {
      if (cancelled) return
      setStatus('connecting')
      socket = new WebSocket(WS_URL)
      ws.current = socket

      socket.onopen = () => {
        if (cancelled || ws.current !== socket) { socket.close(); return }
        setStatus('open')
        subscribedJobsRef.current = { socket, ids: new Set() }
        for (const jobId of pendingJobCancelsRef.current) {
          socket.send(JSON.stringify({ type: 'cancel_job', job_id: jobId }))
        }
        pendingJobCancelsRef.current.clear()
        if (projectId) socket.send(JSON.stringify({ project: projectId }))
        // Re-attach to any job that was still live when the socket dropped;
        // the server replays everything after since_seq.
        for (const job of Object.values(jobsRef.current)) {
          if ((job.status === 'queued' || job.status === 'running') &&
              (!projectId || job.session === projectId)) {
            subscribeJob(job.id)
          }
        }
        // After a full page refresh jobsRef is empty; ask the build registry
        // for jobs still queued/running and attach to those too.
        fetch(`${API_URL}/api/jobs`)
          .then((r) => {
            if (!r.ok) throw new Error('jobs registry request failed')
            return r.json()
          })
          .then((list) => {
            // A navigation/reconnect may have replaced this socket while the
            // registry request was in flight. Ignore that stale snapshot.
            if (cancelled || ws.current !== socket) return
            if (!Array.isArray(list)) throw new Error('jobs registry returned invalid data')
            setJobsState('ready')
            for (const j of list) {
              // Recent FINISHED jobs are listed too (collapsed in the UI; the
              // stored log is replayed on expand). Only live jobs get an
              // immediate subscription.
              updateJob(j.id, (cur) => ({
                ...cur, kind: j.kind ?? cur.kind, design: j.design ?? cur.design,
                status: cur.liveStatusVersion ? cur.status : mergeHydratedStatus(cur.status, j.status),
                outcome: cur.outcome ?? j.outcome,
                session: j.session ?? cur.session,
                created_at: j.created_at ?? cur.created_at,
                started_at: j.started_at ?? cur.started_at,
                finished_at: j.finished_at ?? cur.finished_at,
                returncode: j.returncode ?? cur.returncode,
                error: j.error ?? cur.error,
                startedAt: cur.liveMetadataVersion ? cur.startedAt : (Date.parse(j.created_at) || cur.startedAt),
              }))
              const effectiveStatus = jobsRef.current[j.id]?.status ?? j.status
              if ((effectiveStatus === 'queued' || effectiveStatus === 'running') && (!projectId || j.session === projectId)) subscribeJob(j.id)
            }
          })
          .catch(() => {
            if (!cancelled && ws.current === socket) setJobsState('unavailable')
          })
      }
      socket.onclose = (e) => {
        // Ignore closes of superseded sockets: on navigation the cleanup
        // closes the old socket, but its onclose can fire AFTER the new
        // socket already opened — without this guard the stale close
        // overwrites the live status and the UI sticks on "reconnecting".
        if (cancelled || ws.current !== socket) return
        console.warn(`[agent] websocket closed (code=${e.code}), retrying in 2s — ${WS_URL}`)
        setStatus('closed')
        // Transport loss is not a run terminal state. The backend may still
        // be executing a detached chat; busy is reconciled by the next
        // authoritative `busy`/run-state replay after reconnect.
        retryTimer = setTimeout(connect, 2000)
      }
      socket.onerror = (e) => {
        if (cancelled || ws.current !== socket) return
        console.error('[agent] websocket error', e)
        socket.close()
      }

      socket.onmessage = (event) => {
        if (cancelled || ws.current !== socket) return
        const data = JSON.parse(event.data)

        if (data.type === 'history') {
          // Replay of a persisted project's past conversation. tool_result
          // entries lack a persisted ok flag — recompute it with the same
          // verdict function as live frames so colors don't change on refresh.
          const replayed = data.content.map((m) =>
            m.role === 'tool_result' ? { ...m, ok: toolResultOk(m.content) } : m)
          setMessages([...(replayed.length ? replayed : [GREETING])])
          // The workbench (Verilog viewer, metrics) is restored by the
          // project_state frame the server sends right after the history.
          setVerilog(null)
          setLatestLint(null)
          setHardwareProject(null)
          setMetrics({})
          setMetricProvenance({})
          designContractRef.current = null
          setDesignContract(null)
          setContractActionPending(false)
          setActivity([])
          setRunState(null)
          setStatusText(null)
          reasoningRef.current = EMPTY_REASONING
          setReasoning(EMPTY_REASONING)
          resetGeneration()
          clearTimeline()
          const key = TIMELINE_KEY(projectId)
          if (key) {
            try {
              const saved = JSON.parse(localStorage.getItem(key) || '[]')
              if (Array.isArray(saved) && saved.length) appendTimeline({ type: 'timeline', content: { events: saved } })
            } catch { /* ignore malformed local recovery data */ }
          }
        } else if (data.type === 'project_state') {
          // Persisted workbench state, sent right after the history frame on
          // project join: restores the Verilog viewer and PPA metric cards.
          // Compact source handles are internal model-context references,
          // never dashboard code. Keep the existing full RTL if an older
          // backend/project_state still sends a marker.
          if (data.verilog && !isArtifactHandle(data.verilog.code)) {
            setVerilog(data.verilog)
          }
          if (data.lint) setLatestLint(data.lint)
          if (data.hardware_project) setHardwareProject(data.hardware_project)
          if (data.metrics) setMetrics(data.metrics)
          if (data.metric_provenance) setMetricProvenance(data.metric_provenance)
          if (data.design_contract || data.contract) {
            applyContract({ type: 'contract_upsert', contract: data.design_contract ?? data.contract })
          } else if (data.contract_clear) {
            applyContract({ type: 'contract_clear' })
          }
          if (Array.isArray(data.timeline)) {
            appendTimeline({ type: "timeline", content: { events: data.timeline } })
          }
          if (data.run_state && typeof data.run_state === 'object') {
            applyRunState(data.run_state)
          }
        } else if (data.type === 'busy') {
          // A chat is mid-run server-side (started before we joined — e.g. a
          // detached run surviving a page refresh). The run's buffered events
          // follow this frame; show the working indicator immediately.
          setBusy(true)
          setIsRunning(true)
        } else if (isDesignContractFrame(data)) {
          // Contract surfaces are authoritative backend snapshots/patches.
          applyContract(data)
        } else if (data.type === 'contract_error') {
          setContractActionPending(false)
          setMessages((prev) => [...prev, {
            role: 'error',
            content: `Design contract ${data.code || 'error'}: ${data.error || 'action rejected'}`,
          }])
        } else if (isStructuredTimelineFrame(data)) {
          // Structured progress/decision frames are intentionally kept out of
          // the transcript: render only their concise phase/action/reason/
          // evidence summary in the foldable timeline.
          appendTimeline(data)
          const structuredEvents = normalizeTimelineFrame(data)
          const latest = structuredEvents[structuredEvents.length - 1]
          if (latest?.phase || latest?.action) setStatusText(`${latest.phase}: ${latest.action}`)
          if (isTerminalRunFrame(data) || structuredEvents.some((event) => TERMINAL_RUN_STATES.has(event.status))) {
            setBusy(false)
            setIsRunning(false)
          } else {
            setBusy(true)
            setIsRunning(true)
          }
        } else if (data.type === 'run_state') {
          // S3's persisted lifecycle snapshot is authoritative. Do not infer
          // whole-run completion from an individual generation ending.
          applyRunState(data)
        } else if (data.type === 'status') {
          // LLM-wait heartbeat: show as the working-indicator text until the
          // next real frame arrives. Provider reasoning itself is kept in the
          // folded reasoning tile, never appended to the transcript.
          setStatusText(data.content)
        } else if (data.type === 'generation_start') {
          setBusy(true)
          setIsRunning(true)
          setStatusText(null)
          startGeneration(data)
        } else if (data.type === 'reasoning_delta') {
          setBusy(true)
          setIsRunning(true)
          setStatusText(null)
          appendReasoning(data.content ?? data.delta, data)
        } else if (data.type === 'text_delta') {
          setBusy(true)
          setIsRunning(true)
          setStatusText(null)
          appendTextDelta(data.content ?? data.delta, data)
        } else if (data.type === 'generation_usage' || data.type === 'usage') {
          const usage = data.usage ?? data
          const nextGeneration = { ...generationRef.current, usage }
          generationRef.current = nextGeneration
          setGeneration({
            active: nextGeneration.active,
            round: nextGeneration.round,
            generationId: nextGeneration.generationId,
            usage,
          })
          updateReasoning({ usage })
        } else if (data.type === 'generation_end') {
          const endedId = data.generation_id ?? data.generationId ?? null
          if (endedId && generationRef.current.generationId && endedId !== generationRef.current.generationId) return
          const streamId = generationRef.current.streamId
          if (streamId) {
            setMessages((prev) => prev.map((message) => (
              message.streamId === streamId ? { ...message, streaming: false } : message
            )))
          }
          generationRef.current = { ...generationRef.current, active: false }
          setGeneration({
            active: false,
            round: generationRef.current.round,
            generationId: generationRef.current.generationId,
            usage: generationRef.current.usage,
          })
          updateReasoning({ active: false, usage: generationRef.current.usage })
          setStatusText(null)
        } else if (data.type === 'text') {
          // Legacy complete-response frame. If it belongs to the active
          // generation, complete the single streamed assistant bubble instead
          // of creating a duplicate message.
          setIsRunning(true)
          setStatusText(null)
          const generationId = data.generation_id ?? data.generationId ?? null
          const streamId = generationRef.current.streamId
          if (streamId && (!generationId || generationId === generationRef.current.generationId)) {
            setMessages((prev) => prev.map((message) => (
              message.streamId === streamId
                ? { ...message, content: data.content, streaming: false }
                : message
            )))
            generationRef.current = { ...generationRef.current, active: false, streamId: null }
            setGeneration({ active: false, round: generationRef.current.round, generationId: generationRef.current.generationId, usage: generationRef.current.usage })
          } else {
            setMessages((prev) => [...prev, { role: 'agent', content: data.content, ts: data.ts }])
          }
        } else if (data.type === 'tool_call') {
          setIsRunning(true)
          setStatusText(null)
          let parsed = null
          try { parsed = JSON.parse(data.content) } catch { /* plain string fallback */ }
          const name = parsed?.name ?? data.content
          const args = parsed?.args ?? {}
          // Tool-call args are intentionally compacted for transport. A
          // marker must not overwrite the full source already shown in Code.
          if (typeof args.verilog_code === 'string' &&
              !isArtifactHandle(args.verilog_code)) {
            setVerilog({ top: args.top_module ?? 'top', code: args.verilog_code })
          }
          setMessages((prev) => [...prev, { role: 'tool_call', content: name, ts: data.ts }])
        } else if (data.type === 'tool_result') {
          setIsRunning(true)
          setStatusText(null)
          const ok = toolResultOk(data.content)
          setMessages((prev) => [...prev, { role: 'tool_result', content: data.content, ok, ts: data.ts }])
          { const snapshot = extractMetricSnapshot(data.content); if (Object.keys(snapshot).length) { setMetrics(snapshot); setMetricProvenance(extractMetricProvenance(data.content)) } }
          const cached = /\bcached\s*[:=]\s*true\b|\bcached\b/i.test(data.content)
          setActivity((prev) => [
            { ok, cached, summary: data.content.split('\n')[0], time: new Date() },
            ...prev.slice(0, 49),
          ])
          // Tool results carry a final "job_id: <id>" marker; subscribe in
          // case the job_started broadcast was missed (e.g. history replay).
          const jm = data.content.match(/^job_id:\s*(\S+)/m)
          if (jm) {
            // A REST hydration or an early log frame may have created the
            // placeholder already. Fill its ownership in either case.
            updateJob(jm[1], (j) => ({ ...j, session: j.session ?? projectId }))
            subscribeJob(jm[1])
          }
        } else if (data.type === 'job_started') {
          // Broadcasts are global. Retain every job in the registry and let
          // the dashboard's project filter decide visibility. A foreign job
          // must not auto-switch the current project to Jobs, however.
          const belongsToCurrentProject = !projectId || data.session === projectId
          // fresh: arrived via a LIVE broadcast (not registry rehydration) —
          // the dashboard auto-switches to the Jobs tab for these only.
          updateJob(data.job_id, (j) => ({
            ...j, kind: data.kind, design: data.design,
            session: data.session ?? j.session,
            fresh: belongsToCurrentProject,
          }), true)
          if (belongsToCurrentProject) subscribeJob(data.job_id)
        } else if (data.type === 'tool_log') {
          const seq = Number(data.seq)
          if (Number.isFinite(seq) && (jobsRef.current[data.job_id]?.lineSeqs ?? []).includes(seq)) return
          const lastSeq = jobsRef.current[data.job_id]?.lastSeq ?? 0
          if (Number.isFinite(seq) && seq <= lastSeq) return
          if (Number.isFinite(seq)) {
            try { localStorage.setItem(JOB_SEQ_KEY(data.job_id), String(seq)) } catch { /* storage is optional */ }
          }
          updateJob(data.job_id, (j) => ({
            ...j,
            status: j.status === 'queued' ? 'running' : j.status,
            lastSeq: Number.isFinite(seq) ? Math.max(j.lastSeq ?? 0, seq) : j.lastSeq,
            lineSeqs: Number.isFinite(seq) ? [...(j.lineSeqs ?? []), seq].slice(-800) : [...(j.lineSeqs ?? []), null].slice(-800),
            lines: Number.isFinite(seq) && (j.lineSeqs ?? []).includes(seq) ? j.lines : [...j.lines, data.line].slice(-800),
          }), true)
        } else if (data.type === 'log_gap') {
          updateJob(data.job_id, (j) => ({
            ...j,
            lines: [...j.lines, `… log gap: output lost, resuming at seq ${data.from_seq} …`].slice(-800),
            lineSeqs: [...(j.lineSeqs ?? []), `gap-${data.from_seq}`].slice(-800),
          }), true)
        } else if (data.type === 'job_status') {
          updateJob(data.job_id, (j) => ({
            ...j, status: mergeLiveStatus(j.status, data.status), outcome: data.outcome ?? j.outcome,
            manifest: data.manifest ?? j.manifest,
          }), true)
          if (data.status === 'done' && data.manifest) {
            const source = data.manifest.metrics && typeof data.manifest.metrics === 'object' ? data.manifest.metrics : data.manifest
            const text = `${JSON.stringify(data.manifest)}\n${Object.entries(source).map(([k, v]) => `${k}: ${v}`).join('\n')}`
            const snapshot = extractMetricSnapshot(text)
            if (Object.keys(snapshot).length) { setMetrics(snapshot); setMetricProvenance(extractMetricProvenance(text)) }
          }
        } else if (data.type === 'error') {
          setMessages((prev) => [...prev, { role: 'error', content: data.content, ts: data.ts }])
          setBusy(false)
          setIsRunning(false)
          setStatusText(null)
        } else if (data.type === 'end') {
          setBusy(false)
          setIsRunning(false)
          setStatusText(null)
          resetGeneration()
          updateReasoning({ active: false })
        }
      }
    }

    connect()
    return () => {
      cancelled = true
      clearTimeout(retryTimer)
      socket?.close()
    }
  }, [appendReasoning, appendTextDelta, appendTimeline, applyContract, applyRunState, clearTimeline, projectId, resetGeneration, startGeneration, subscribeJob, updateJob, updateReasoning])

  const send = useCallback((text) => {
    if (!text.trim()) return false
    if (!ws.current || ws.current.readyState !== WebSocket.OPEN) return false
    // Once the backend has published a contract, normal chat input is paused
    // until the typed approval/revision flow resolves. Approval starts the
    // saved request; the browser never resends the user's original prompt.
    if (contractGate(designContractRef.current, { busy, isRunning }).blocked) return false
    setMessages((prev) => [...prev, { role: 'user', content: text, ts: Date.now() / 1000 }])
    ws.current.send(text)
    setBusy(true)
    setRunState(null)
    setStatusText(null)
    reasoningRef.current = EMPTY_REASONING
    setReasoning(EMPTY_REASONING)
    resetGeneration()
    clearTimeline()
    return true
  }, [busy, clearTimeline, isRunning, resetGeneration])

  const sendCancel = useCallback(() => {
    if (!ws.current || ws.current.readyState !== WebSocket.OPEN) return
    ws.current.send(JSON.stringify({ type: 'cancel' }))
  }, [])

  const sendContractAction = useCallback((action, payload = {}) => {
    if (!action || !ws.current || ws.current.readyState !== WebSocket.OPEN) return false
    const contract = designContractRef.current
    if (!projectId || !contract?.revision || !contract?.digest || contractActionPending) return false
    const gate = contractGate(contract)
    if (action === 'contract.approve_and_build' && !gate.canApprove) return false
    ws.current.send(JSON.stringify({
      type: 'contract_action',
      project: projectId,
      action,
      expected_revision: contract.revision,
      expected_digest: contract.digest,
      payload,
    }))
    setContractActionPending(true)
    return true
  }, [contractActionPending, projectId])

  return { status, model, messages, busy, isRunning, statusText, runState, generation, reasoning, timeline, metrics, metricProvenance, verilog, latestLint, hardwareProject, designContract, contractActionPending, activity, jobs, jobsState, send, sendCancel, sendContractAction, subscribeJob, cancelJob }
}

// ---- Project list REST API ----

export async function fetchProjects() {
  const r = await fetch(`${API_URL}/api/projects`)
  return r.ok ? r.json() : []
}

export async function fetchModels() {
  const r = await fetch(`${API_URL}/api/models`)
  return r.ok ? r.json() : []
}

export async function createProject(name, model) {
  const r = await fetch(`${API_URL}/api/projects`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(model ? { name, model } : { name }),
  })
  return r.ok ? r.json() : null
}

export async function renameProject(id, name) {
  const r = await fetch(`${API_URL}/api/projects/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  return r.ok ? r.json() : null
}

export async function setProjectModel(id, model) {
  const r = await fetch(`${API_URL}/api/projects/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model }),
  })
  return r.ok ? r.json() : null
}

export async function setProjectEffort(id, effort) {
  const r = await fetch(`${API_URL}/api/projects/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reasoning_effort: effort }),
  })
  return r.ok ? r.json() : null
}

export async function deleteProject(id) {
  await fetch(`${API_URL}/api/projects/${id}`, { method: 'DELETE' })
}

// ---- Gate-netlist artifact API (Phase G1/G2) ----
// Both helpers resolve to a structured result rather than throwing, so the
// schematic viewer can render honest loading/error/unavailable states.

export async function fetchJobNetlist(jobId) {
  const r = await fetch(`${API_URL}/api/jobs/${jobId}/netlist`)
  if (!r.ok) return { ok: false, status: r.status, error: `netlist metadata request failed (${r.status})` }
  return { ok: true, data: await r.json() }
}

export async function fetchJobNetlistGraph(jobId, params = {}) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') query.set(key, value)
  }
  const r = await fetch(`${API_URL}/api/jobs/${jobId}/netlist/graph?${query}`)
  if (!r.ok) {
    let detail = `graph request failed (${r.status})`
    try { detail = (await r.json()).detail ?? detail } catch { /* keep default */ }
    return { ok: false, status: r.status, error: detail }
  }
  return { ok: true, data: await r.json() }
}

export async function fetchJobTimingPaths(jobId, netlistSha256) {
  const query = netlistSha256 ? `?netlist_sha256=${netlistSha256}` : ''
  const r = await fetch(`${API_URL}/api/jobs/${jobId}/timing-paths${query}`)
  if (!r.ok) {
    let detail = `timing path request failed (${r.status})`
    try { detail = (await r.json()).detail ?? detail } catch { /* keep default */ }
    return { ok: false, status: r.status, error: detail }
  }
  return { ok: true, data: await r.json() }
}

export async function fetchJobLayout(jobId) {
  const r = await fetch(`${API_URL}/api/jobs/${jobId}/layout`)
  if (!r.ok) {
    let detail = `layout request failed (${r.status})`
    try { detail = (await r.json()).detail ?? detail } catch { /* keep default */ }
    return { ok: false, status: r.status, error: detail }
  }
  return { ok: true, data: await r.json() }
}

// ---- Mistral setup wizard (mirrors `vibe --setup` over REST) ----
// The API key never reaches this side: the browser flow is exchanged
// server-side and stored in the backend's .env.

export async function mistralSetupState() {
  const r = await fetch(`${API_URL}/api/providers/mistral/setup/state`)
  return r.ok ? r.json() : null
}

export async function mistralBrowserStart(consoleBaseUrl) {
  const r = await fetch(`${API_URL}/api/providers/mistral/setup/browser/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(consoleBaseUrl ? { console_base_url: consoleBaseUrl } : {}),
  })
  return r.ok ? { ok: true, data: await r.json() } : { ok: false, error: (await r.json()).detail }
}

export async function mistralBrowserStatus() {
  const r = await fetch(`${API_URL}/api/providers/mistral/setup/browser/status`)
  return r.ok ? r.json() : { status: 'error', message: 'status request failed' }
}

export async function mistralBrowserComplete() {
  const r = await fetch(`${API_URL}/api/providers/mistral/setup/browser/complete`, { method: 'POST' })
  return r.ok ? { ok: true, data: await r.json() } : { ok: false, error: (await r.json()).detail }
}

export async function mistralSubmitKey(apiKey) {
  const r = await fetch(`${API_URL}/api/providers/mistral/setup/key`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_key: apiKey }),
  })
  return r.ok ? { ok: true, data: await r.json() } : { ok: false, error: (await r.json()).detail }
}
