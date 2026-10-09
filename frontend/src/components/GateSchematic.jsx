import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Crosshair, Maximize2, RotateCcw, Search, ZoomIn, ZoomOut, ChevronLeft } from 'lucide-react'
import { fetchJobNetlist, fetchJobNetlistGraph, fetchJobTimingPaths } from '../hooks/useAgentSocket'

// Bounded gate-level schematic (Phase G2). Everything rendered comes from the
// canonical gate-netlist artifact served by the bounded graph endpoint; when
// the artifact is absent the panel says so instead of inventing a diagram.

const DEFAULT_NODE_LIMIT = 500
const MAX_NODE_LIMIT = 2000
const MIN_SCALE = 0.08
const MAX_SCALE = 4

const SEQ_CELL = /df|latch|sram|reg/i

function parseSourceAttr(source) {
  // "counter8.v:12" (or a path without a line) -> { file, line }
  if (!source) return null
  const index = source.lastIndexOf(':')
  if (index <= 0) return { file: source, line: null }
  const line = Number(source.slice(index + 1))
  return Number.isInteger(line) && line > 0
    ? { file: source.slice(0, index), line }
    : { file: source, line: null }
}

function useReducedMotion() {
  const [reduced, setReduced] = useState(false)
  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    const update = () => setReduced(query.matches)
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  return reduced
}

// Layout runs in a worker so a large bounded graph can never block chat,
// job streaming, or cancellation (Phase G2 requirement).
function useLayoutWorker() {
  const workerRef = useRef(null)
  const pendingRef = useRef(new Map())
  useEffect(() => {
    const worker = new Worker(new URL('../workers/graphLayout.worker.js', import.meta.url), { type: 'module' })
    worker.onmessage = (event) => {
      const { id, layout, error } = event.data ?? {}
      const pending = pendingRef.current.get(id)
      if (!pending) return
      pendingRef.current.delete(id)
      if (error) pending.reject(new Error(error))
      else pending.resolve(layout)
    }
    worker.onerror = () => {
      for (const pending of pendingRef.current.values()) pending.reject(new Error('layout worker failed'))
      pendingRef.current.clear()
    }
    workerRef.current = worker
    return () => {
      worker.terminate()
      workerRef.current = null
    }
  }, [])
  return useCallback((graph) => new Promise((resolve, reject) => {
    const worker = workerRef.current
    if (!worker) {
      reject(new Error('layout worker unavailable'))
      return
    }
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`
    pendingRef.current.set(id, { resolve, reject })
    worker.postMessage({ id, graph })
  }), [])
}

function shortDigest(digest) {
  return typeof digest === 'string' ? digest.slice(0, 10) : '—'
}

function pinSide(node, pin) {
  const direction = node.pin_directions?.[pin]
  if (direction === 'input') return 'left'
  if (direction === 'output') return 'right'
  return null
}

function CellGlyph({ node, pos, selected, dimmed, pathHighlighted, onSelect, reduceMotion }) {
  const side = pos
  const isSequential = SEQ_CELL.test(node.type ?? '')
  const pins = Object.keys(node.pins ?? {})
  const leftPins = pins.filter((pin) => pinSide(node, pin) === 'left')
  const rightPins = pins.filter((pin) => pinSide(node, pin) === 'right')
  const stroke = node.black_box ? '#525252' : '#3f3f46'
  const dash = node.black_box ? '3 2' : undefined
  return (
    <g
      transform={`translate(${side.x}, ${side.y})`}
      opacity={dimmed ? 0.25 : 1}
      onClick={(event) => { event.stopPropagation(); onSelect(node, 'node') }}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect(node, 'node')
        }
      }}
      tabIndex={0}
      role="button"
      aria-label={`${node.kind === 'cell' ? 'cell' : 'port'} ${node.name}${node.type ? `, type ${node.type}` : ''}${node.hierarchy ? `, hierarchy ${node.hierarchy}` : ''}`}
      className="cursor-pointer focus:outline-none"
    >
      {node.kind === 'port' ? (
        <>
          <rect width={pos.w} height={pos.h} fill="#0d0d0d" stroke={stroke} strokeWidth={1} rx={2} strokeDasharray={dash} />
          <text x={6} y={13} fill="#8b9dc3" fontSize={9} fontFamily="ui-monospace, monospace">{node.name}</text>
          {node.direction === 'input' && <path d={`M ${pos.w - 8} 6 L ${pos.w - 2} ${pos.h / 2} L ${pos.w - 8} ${pos.h - 6}`} fill="#4b5563" />}
          {node.direction === 'output' && <path d={`M 8 6 L 2 ${pos.h / 2} L 8 ${pos.h - 6}`} fill="#4b5563" />}
        </>
      ) : (
        <>
          {selected && (
            <rect x={-4} y={-4} width={pos.w + 8} height={pos.h + 8} fill="none" stroke="#34d399" strokeWidth={1.2} rx={3} />
          )}
          {pathHighlighted && !selected && (
            <rect x={-3.5} y={-3.5} width={pos.w + 7} height={pos.h + 7} fill="none" stroke="#fbbf24" strokeWidth={1} rx={3} />
          )}
          <rect width={pos.w} height={pos.h} fill="#161616" stroke={selected ? '#34d399' : stroke} strokeWidth={1} rx={2} strokeDasharray={dash} />
          {node.hierarchy && (
            <rect x={3} y={3} width={pos.w - 6} height={pos.h - 6} fill="none" stroke={stroke} strokeWidth={0.75} rx={1} />
          )}
          {isSequential && (
            <path d={`M 0 ${pos.h / 2 - 4} L 7 ${pos.h / 2} L 0 ${pos.h / 2 + 4} Z`} fill="#0d0d0d" stroke={stroke} strokeWidth={0.9} />
          )}
          <text x={16} y={12} fill="#d4d4d4" fontSize={9} fontFamily="ui-monospace, monospace">{node.name.slice(0, 12)}</text>
          <text x={16} y={23} fill="#6b7280" fontSize={7.5} fontFamily="ui-monospace, monospace">{(node.type ?? '').replace(/^sky130_fd_sc_hd__/, '').slice(0, 16)}</text>
          {leftPins.map((pin, index) => (
            <g key={`l-${pin}`}>
              <line x1={-5} y1={7 + index * 5} x2={0} y2={7 + index * 5} stroke="#525252" strokeWidth={1} />
              <rect x={-6} y={6 + index * 5} width={2} height={2} fill="#737373" />
              {leftPins.length <= 4 && <text x={2} y={6 + index * 5} fill="#525252" fontSize={5} fontFamily="ui-monospace, monospace">{pin.slice(0, 4)}</text>}
            </g>
          ))}
          {rightPins.map((pin, index) => (
            <g key={`r-${pin}`}>
              <line x1={pos.w} y1={7 + index * 5} x2={pos.w + 5} y2={7 + index * 5} stroke="#525252" strokeWidth={1} />
              <rect x={pos.w + 4} y={6 + index * 5} width={2} height={2} fill="#737373" />
              {rightPins.length <= 4 && <text x={pos.w - 2} y={6 + index * 5} textAnchor="end" fill="#525252" fontSize={5} fontFamily="ui-monospace, monospace">{pin.slice(0, 4)}</text>}
            </g>
          ))}
        </>
      )}
      {node.kind === 'port' && selected && (
        <rect x={-3} y={-3} width={pos.w + 6} height={pos.h + 6} fill="none" stroke="#34d399" strokeWidth={1.2} rx={3} />
      )}
      {reduceMotion ? null : undefined}
    </g>
  )
}

export default function GateSchematic({ job, onOpenSource, onSelectionChange, onPathChange, externalSelection }) {
  const reduceMotion = useReducedMotion()
  const layoutGraph = useLayoutWorker()

  const [meta, setMeta] = useState(null)      // artifact summary | unavailable record
  const [metaError, setMetaError] = useState(null)
  const [scope, setScope] = useState(null)    // { module, limit, cone }
  const [breadcrumb, setBreadcrumb] = useState([])
  const [graph, setGraph] = useState(null)
  const [layout, setLayout] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [selection, setSelection] = useState(null) // { type: 'node'|'net', id }
  const [view, setView] = useState({ x: 0, y: 0, scale: 1 })
  const [search, setSearch] = useState('')
  // Phase G3: ordered timing paths from the exact STA run, mapped onto this
  // netlist. Selection is rejected unless the digests match.
  const [timingPaths, setTimingPaths] = useState(null)
  const [selectedPathIndex, setSelectedPathIndex] = useState(null)
  const [pathMismatch, setPathMismatch] = useState(null)
  const containerRef = useRef(null)
  const svgRef = useRef(null)
  const dragRef = useRef(null)
  const graphRequestIdRef = useRef(0)
  // Container geometry is state (via ResizeObserver), never a ref read
  // during render: fit, minimap, and viewport math all consume it.
  const [containerSize, setContainerSize] = useState(null)
  useEffect(() => {
    const container = containerRef.current
    if (!container) return undefined
    const update = () => setContainerSize({ w: container.clientWidth, h: container.clientHeight })
    update()
    const observer = new ResizeObserver(update)
    observer.observe(container)
    return () => observer.disconnect()
  }, [])

  const jobId = job?.id ?? null

  const resetForJob = useCallback((summary) => {
    setScope(null)
    setBreadcrumb([])
    setGraph(null)
    setLayout(null)
    setSelection(null)
    setError(null)
    if (summary?.available) {
      setScope({ module: summary.top_module, limit: DEFAULT_NODE_LIMIT, cone: null })
      setBreadcrumb([{ module: summary.top_module, label: summary.top_module }])
    }
  }, [])

  // Artifact metadata follows the selected job. A job change invalidates
  // incompatible selections and scope explicitly (Phase G2 acceptance).
  useEffect(() => {
    if (!jobId) {
      // Job changes invalidate evidence-bound state before the async fetch.
      // oxlint-disable-next-line react/set-state-in-effect
      setMeta(null)
      setMetaError(null)
      resetForJob(null)
      return undefined
    }
    let cancelled = false
    setLoading(true)
    setMeta(null)
    setMetaError(null)
    resetForJob(null)
    fetchJobNetlist(jobId).then((result) => {
      if (cancelled) return
      setLoading(false)
      if (!result.ok) {
        setMetaError(result.error)
        return
      }
      const summary = result.data
      setMeta(summary)
      resetForJob(summary)
    })
    return () => { cancelled = true }
  }, [jobId, resetForJob])

  // Timing paths follow the artifact identity (Phase G3).
  useEffect(() => {
    if (!jobId || !meta?.available) {
      // Timing paths are invalid until the new artifact identity is loaded.
      // oxlint-disable-next-line react/set-state-in-effect
      setTimingPaths(null)
      setSelectedPathIndex(null)
      setPathMismatch(null)
      return undefined
    }
    let cancelled = false
    setTimingPaths(null)
    setSelectedPathIndex(null)
    setPathMismatch(null)
    fetchJobTimingPaths(jobId).then((result) => {
      if (cancelled) return
      if (result.ok) setTimingPaths(result.data)
    })
    return () => { cancelled = true }
  }, [jobId, meta?.available])

  // Bounded graph fetch for the current scope.
  useEffect(() => {
    if (!jobId || !meta?.available || !scope) return undefined
    const requestId = graphRequestIdRef.current + 1
    graphRequestIdRef.current = requestId
    let cancelled = false
    // Clear the previous request error before loading the new bounded cone.
    // oxlint-disable-next-line react/set-state-in-effect
    setError(null)
    setLoading(true)
    fetchJobNetlistGraph(jobId, {
      module: scope.module,
      limit: scope.limit,
      cone: scope.cone ?? undefined,
      cone_hops: scope.cone ? 16 : undefined,
    }).then(async (result) => {
      if (cancelled || requestId !== graphRequestIdRef.current) return
      setLoading(false)
      if (!result.ok) {
        setError(result.error)
        setGraph(null)
        setLayout(null)
        return
      }
      setGraph(result.data)
      // Layout in the worker; a stale response is dropped by request id.
      try {
        const computed = await layoutGraph(result.data)
        if (cancelled || requestId !== graphRequestIdRef.current) return
        setLayout(computed)
      } catch (layoutError) {
        if (!cancelled) setError(String(layoutError))
      }
    })
    return () => { cancelled = true }
  }, [jobId, meta?.available, scope, layoutGraph])

  const nodeById = useMemo(() => new Map((graph?.nodes ?? []).map((node) => [node.id, node])), [graph])
  const netById = useMemo(() => new Map((graph?.nets ?? []).map((net) => [net.id, net])), [graph])
  const positionById = useMemo(() => new Map((layout?.positions ?? []).map((item) => [item.id, item])), [layout])
  const routes = useMemo(() => layout?.routes ?? [], [layout])

  const fit = useCallback(() => {
    // A hidden (display:none) container reports zero size; fitting against
    // it would produce a garbage transform. Refit happens when the surface
    // becomes visible and the ResizeObserver reports a real size.
    if (!containerSize || !containerSize.w || !containerSize.h || !layout || !layout.size) return
    const scale = Math.min(
      Math.max(Math.min(containerSize.w / layout.size.w, containerSize.h / layout.size.h) * 0.92, MIN_SCALE),
      MAX_SCALE)
    setView({
      scale,
      x: (containerSize.w - layout.size.w * scale) / 2,
      y: (containerSize.h - layout.size.h * scale) / 2,
    })
  }, [layout, containerSize])

  // Fit each newly laid out graph (once), keeping the user's later zoom.
  // The component stays mounted while other workbench tabs are active, so a
  // fit computed against a zero-size (hidden) canvas is repeated as soon as
  // the surface becomes visible.
  const fittedForRef = useRef(null)
  const prevSizeRef = useRef(null)
  useEffect(() => {
    const wasHidden = prevSizeRef.current != null
      && (prevSizeRef.current.w === 0 || prevSizeRef.current.h === 0)
    prevSizeRef.current = containerSize
    const visible = containerSize && containerSize.w > 0 && containerSize.h > 0
    if (layout && graph && visible && (fittedForRef.current !== graph || wasHidden)) {
      fittedForRef.current = graph
      fit()
    }
  }, [layout, graph, containerSize, fit])

  const centerOn = useCallback((position) => {
    if (!containerSize || !position) return
    setView((current) => ({
      ...current,
      x: containerSize.w / 2 - (position.x + position.w / 2) * current.scale,
      y: containerSize.h / 2 - (position.y + position.h / 2) * current.scale,
    }))
  }, [containerSize])

  const selectNode = useCallback((node) => {
    setSelection({ type: 'node', id: node.id })
    const position = positionById.get(node.id)
    if (position) centerOn(position)
  }, [positionById, centerOn])

  const selectNet = useCallback((netId) => {
    setSelection({ type: 'net', id: netId })
  }, [])

  // Highlight model: a net selection lights its own wires; a node selection
  // lights its wires and direct neighbours. Everything else dims.
  // Phase G3: the selected timing path defines the highlight model. Setup and
  // hold paths, corners, and clocks stay distinguishable everywhere.
  const selectedPath = selectedPathIndex != null && timingPaths?.paths
    ? timingPaths.paths[selectedPathIndex] : null
  const pathHighlight = useMemo(() => {
    if (!selectedPath) return null
    const nodes = new Set()
    const nets = new Set()
    for (const segment of selectedPath.segments ?? []) {
      if (!segment.graph_id) continue
      if (segment.graph_id.startsWith('net:')) nets.add(segment.graph_id)
      else nodes.add(segment.graph_id)
    }
    return { nodes, nets }
  }, [selectedPath])

  const selectPath = (index) => {
    const path = timingPaths?.paths?.[index]
    if (!path) return
    // Digest gate: a path is only meaningful on the exact netlist it was
    // measured against; a mismatch is rejected, never silently combined.
    if (timingPaths?.identity?.netlist_sha256 && meta?.netlist_sha256
        && timingPaths.identity.netlist_sha256 !== meta.netlist_sha256) {
      setPathMismatch('these timing paths belong to a different netlist — highlighting rejected')
      setSelectedPathIndex(null)
      return
    }
    setPathMismatch(null)
    setSelectedPathIndex(index)
    setSelection(null)
    // Open the smallest useful cone: the first mapped cell of the path.
    const firstCell = (path.segments ?? []).find((seg) => seg.graph_id?.startsWith('cell:'))
    if (firstCell) setScope((current) => ({ ...current, cone: firstCell.graph_id }))
  }
  const clearPath = () => {
    setSelectedPathIndex(null)
    setPathMismatch(null)
    setScope((current) => ({ ...current, cone: null }))
  }

  const emphasis = useMemo(() => {
    if (pathHighlight) return { routes: pathHighlight.nets, nodes: pathHighlight.nodes }
    if (!selection) return null
    if (selection.type === 'net') {
      const net = netById.get(selection.id)
      const nodes = new Set([...(net?.drivers ?? []), ...(net?.loads ?? [])].map((pin) => pin.node))
      return { routes: new Set([selection.id]), nodes }
    }
    const nodeRoutes = new Set(routes.filter((route) => route.from === selection.id || route.to === selection.id).map((route) => route.net))
    const nodes = new Set([selection.id])
    for (const route of routes) {
      if (route.from === selection.id) nodes.add(route.to)
      if (route.to === selection.id) nodes.add(route.from)
    }
    return { routes: nodeRoutes, nodes }
  }, [selection, routes, netById, pathHighlight])

  const openScope = (module, label) => {
    setScope((current) => ({ ...current, module, cone: null }))
    setBreadcrumb((current) => [...current, { module, label: label ?? module }])
    setSelection(null)
  }
  const goBack = () => {
    setBreadcrumb((current) => {
      if (current.length <= 1) return current
      const next = current.slice(0, -1)
      setScope((scopeCurrent) => ({ ...scopeCurrent, module: next[next.length - 1].module, cone: null }))
      return next
    })
    setSelection(null)
  }

  const openCone = (rootId) => {
    setScope((current) => ({ ...current, cone: rootId }))
    setSelection(null)
  }
  const clearCone = () => {
    setScope((current) => ({ ...current, cone: null }))
  }

  const sourceOfSelection = useMemo(() => {
    if (selection?.type !== 'node') return null
    const node = nodeById.get(selection.id)
    return parseSourceAttr(node?.source)
  }, [selection, nodeById])

  const searchResults = useMemo(() => {
    const query = search.trim().toLowerCase()
    if (!query || !graph) return []
    const results = []
    for (const node of graph.nodes ?? []) {
      const haystack = [node.name, node.type, node.hierarchy, node.source, node.module]
        .filter(Boolean).join(' ').toLowerCase()
      if (haystack.includes(query)) results.push({ type: 'node', id: node.id, label: node.name, sub: node.type ?? node.direction })
    }
    for (const net of graph.nets ?? []) {
      const haystack = (net.names ?? []).join(' ').toLowerCase()
      if (haystack.includes(query)) results.push({ type: 'net', id: net.id, label: net.name, sub: 'net' })
    }
    return results.slice(0, 12)
  }, [search, graph])

  // ---- pointer pan / wheel zoom ----
  const onPointerDown = (event) => {
    if (event.button !== 0) return
    // Panning starts only on the canvas background: pointer capture would
    // retarget a click that begins on a node glyph and make it unselectable.
    if (event.target.closest?.('[role="button"]')) return
    dragRef.current = { startX: event.clientX, startY: event.clientY, viewX: view.x, viewY: view.y }
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const onPointerMove = (event) => {
    const drag = dragRef.current
    if (!drag) return
    setView((current) => ({
      ...current,
      x: drag.viewX + (event.clientX - drag.startX),
      y: drag.viewY + (event.clientY - drag.startY),
    }))
  }
  const onPointerUp = (event) => {
    dragRef.current = null
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }
  const onWheel = (event) => {
    event.preventDefault()
    const container = containerRef.current
    if (!container) return
    const rect = container.getBoundingClientRect()
    const mx = event.clientX - rect.left
    const my = event.clientY - rect.top
    setView((current) => {
      const factor = Math.exp(-event.deltaY * 0.0015)
      const scale = Math.min(Math.max(current.scale * factor, MIN_SCALE), MAX_SCALE)
      const ratio = scale / current.scale
      return { scale, x: mx - (mx - current.x) * ratio, y: my - (my - current.y) * ratio }
    })
  }
  useEffect(() => {
    const container = containerRef.current
    if (!container) return undefined
    const handler = (event) => onWheel(event)
    container.addEventListener('wheel', handler, { passive: false })
    return () => container.removeEventListener('wheel', handler)
  })

  // ---- keyboard: arrows move the selection to the nearest node ----
  const onKeyDown = (event) => {
    if (!positionById.size) return
    if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', '+', '-', 'f'].includes(event.key)) return
    if (event.key === 'f') { fit(); return }
    if (event.key === '+') {
      setView((current) => ({ ...current, scale: Math.min(current.scale * 1.2, MAX_SCALE) }))
      return
    }
    if (event.key === '-') {
      setView((current) => ({ ...current, scale: Math.max(current.scale / 1.2, MIN_SCALE) }))
      return
    }
    event.preventDefault()
    const currentId = selection?.type === 'node' ? selection.id : null
    const currentPos = currentId ? positionById.get(currentId) : null
    let best = null
    let bestScore = Infinity
    for (const [id, position] of positionById) {
      if (id === currentId) continue
      let dx = position.x - (currentPos?.x ?? 0)
      let dy = position.y - (currentPos?.y ?? 0)
      if (event.key === 'ArrowRight') { if (dx <= 0) continue; dy = Math.abs(dy) }
      if (event.key === 'ArrowLeft') { if (dx >= 0) continue; dx = Math.abs(dx); dy = Math.abs(dy) }
      if (event.key === 'ArrowDown') { if (dy <= 0) continue; dx = Math.abs(dx) }
      if (event.key === 'ArrowUp') { if (dy >= 0) continue; dx = Math.abs(dx); dy = Math.abs(dy) }
      const score = dx * dx + dy * dy
      if (score < bestScore) { bestScore = score; best = id }
    }
    if (best) selectNode(nodeById.get(best))
  }

  const minimap = useMemo(() => {
    if (!layout?.size) return null
    const box = { w: 150, h: 96 }
    const scale = Math.min(box.w / layout.size.w, box.h / layout.size.h)
    return { box, scale }
  }, [layout])

  const viewportRect = useMemo(() => {
    if (!minimap || !containerSize) return null
    return {
      x: (-view.x / view.scale) * minimap.scale,
      y: (-view.y / view.scale) * minimap.scale,
      w: (containerSize.w / view.scale) * minimap.scale,
      h: (containerSize.h / view.scale) * minimap.scale,
    }
  }, [view, minimap, containerSize])

  // Cross-view sync (Phase G4): the layout view shares this selection, and a
  // component clicked in the layout selects the same cell here.
  useEffect(() => {
    onSelectionChange?.(selection)
  }, [selection, onSelectionChange])
  useEffect(() => {
    onPathChange?.(pathHighlight ? [...pathHighlight.nodes] : null)
  }, [pathHighlight, onPathChange])
  useEffect(() => {
    if (!externalSelection?.id) return
    const node = nodeById.get(externalSelection.id)
    // Layout cross-probing is an external selection event.
    // oxlint-disable-next-line react/set-state-in-effect
    if (node) selectNode(node)
  }, [externalSelection, nodeById, selectNode])

  // ---- render ----
  if (!jobId) {
    return (
      <div data-testid="gate-schematic" className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center font-mono">
        <div className="text-sm text-gray-500">No job selected</div>
        <div className="max-w-md text-[10px] leading-relaxed text-gray-700">Select a synthesis job to open its evidence-bound gate-level schematic.</div>
      </div>
    )
  }
  if (metaError) {
    return (
      <div data-testid="gate-schematic" className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center font-mono">
        <div className="text-sm text-red-400" data-testid="gate-schematic-error">Schematic unavailable</div>
        <div className="max-w-md text-[10px] leading-relaxed text-gray-600">{metaError}</div>
      </div>
    )
  }
  if (meta && !meta.available) {
    return (
      <div data-testid="gate-schematic" className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center font-mono">
        <div className="text-sm text-gray-500" data-testid="gate-schematic-unavailable">No gate netlist for this job</div>
        <div className="max-w-md text-[10px] leading-relaxed text-gray-700" data-testid="gate-schematic-reason">{meta.reason}</div>
        <div className="max-w-md text-[10px] leading-relaxed text-gray-700">Only a successful synthesis with structured netlist output produces a schematic; none is fabricated from RTL declarations.</div>
      </div>
    )
  }

  const selectedNode = selection?.type === 'node' ? nodeById.get(selection.id) : null
  const selectedNet = selection?.type === 'net' ? netById.get(selection.id) : null

  return (
    <div data-testid="gate-schematic" className="flex h-full min-h-0 flex-col font-mono">
      {/* Identity strip: bound to job, artifact, and netlist digests. */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-gray-800 bg-[#0f0f0f] px-3 py-1.5 text-[9px] text-gray-500">
        <span className="uppercase tracking-wider text-gray-400">gate netlist</span>
        {meta?.top_module && <span>top <b className="font-normal text-gray-300">{meta.top_module}</b></span>}
        {meta?.library && <span>lib <b className="font-normal text-gray-300">{meta.library}</b></span>}
        {meta?.tool && <span>{meta.tool}{meta.tool_version ? ` ${meta.tool_version}` : ''}</span>}
        {meta?.artifact_sha256 && <span title={meta.artifact_sha256}>artifact {shortDigest(meta.artifact_sha256)}</span>}
        {meta?.netlist_sha256 && <span title={meta.netlist_sha256}>netlist {shortDigest(meta.netlist_sha256)}</span>}
        {meta?.cell_count != null && <span>{meta.cell_count} cells</span>}
        {meta?.completeness?.black_box_modules?.length > 0 && (
          <span className="text-orange-400" title={meta.completeness.black_box_modules.join(', ')}>
            {meta.completeness.black_box_modules.length} black-box module(s)
          </span>
        )}
        {meta?.completeness?.unsupported_primitives?.length > 0 && (
          <span className="text-amber-400" title={meta.completeness.unsupported_primitives.join(', ')}>
            {meta.completeness.unsupported_primitives.length} primitive(s) with unknown pin directions
          </span>
        )}
      </div>

      {/* Scope toolbar */}
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-gray-800 bg-[#0d0d0d] px-3 py-1.5">
        <div className="flex items-center gap-1">
          {breadcrumb.length > 1 && (
            <button type="button" onClick={goBack} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Back to parent scope" aria-label="Back to parent scope">
              <ChevronLeft className="h-3 w-3" />
            </button>
          )}
          <span className="text-[10px] text-gray-300" data-testid="gate-schematic-scope">
            {breadcrumb.map((item, index) => (
              <span key={`${item.module}-${index}`}>
                {index > 0 && <span className="text-gray-700"> / </span>}
                <span className={index === breadcrumb.length - 1 ? 'text-gray-100' : 'text-gray-500'}>{item.label}</span>
              </span>
            ))}
          </span>
        </div>
        {(graph?.modules?.length ?? 0) > 1 && (
          <select
            value={scope?.module ?? ''}
            onChange={(event) => openScope(event.target.value)}
            className="rounded border border-gray-800 bg-[#090909] px-1.5 py-0.5 text-[9px] text-gray-400 outline-none"
            aria-label="Hierarchy scope"
          >
            {(graph?.modules ?? []).map((module) => (
              <option key={module} value={module}>{module}</option>
            ))}
          </select>
        )}
        {scope?.cone && (
          <span className="flex items-center gap-1 rounded border border-amber-500/40 px-1.5 py-0.5 text-[9px] text-amber-300" data-testid="gate-schematic-cone">
            cone: {scope.cone}
            <button type="button" onClick={clearCone} className="text-amber-400 hover:text-amber-200" title="Clear cone restriction">clear</button>
          </span>
        )}
        <div className="relative ml-auto">
          <label className="flex items-center gap-1 rounded border border-gray-800 bg-[#090909] px-2">
            <Search className="h-3 w-3 text-gray-600" />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search instances, cells, signals, sources"
              aria-label="Search instances, cells, signals, sources"
              className="w-52 bg-transparent py-1 text-[10px] text-gray-300 outline-none placeholder:text-gray-700"
            />
          </label>
          {searchResults.length > 0 && (
            <div className="absolute right-0 top-7 z-20 w-64 rounded border border-gray-700 bg-[#151515] p-1 shadow-xl" data-testid="gate-schematic-search-results">
              {searchResults.map((result) => (
                <button
                  key={`${result.type}:${result.id}`}
                  type="button"
                  className="block w-full truncate rounded px-2 py-1 text-left text-[10px] text-gray-300 hover:bg-white/5"
                  onClick={() => {
                    setSearch('')
                    if (result.type === 'node') selectNode(nodeById.get(result.id))
                    else selectNet(result.id)
                  }}
                >
                  {result.label} <span className="text-gray-600">{result.sub}</span>
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button type="button" onClick={() => setView((v) => ({ ...v, scale: Math.min(v.scale * 1.25, MAX_SCALE) }))} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Zoom in" aria-label="Zoom in"><ZoomIn className="h-3 w-3" /></button>
          <button type="button" onClick={() => setView((v) => ({ ...v, scale: Math.max(v.scale / 1.25, MIN_SCALE) }))} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Zoom out" aria-label="Zoom out"><ZoomOut className="h-3 w-3" /></button>
          <button type="button" onClick={fit} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Fit to view" aria-label="Fit to view"><Maximize2 className="h-3 w-3" /></button>
          <button type="button" onClick={() => { setView({ x: 0, y: 0, scale: 1 }); fit() }} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Reset view" aria-label="Reset view"><RotateCcw className="h-3 w-3" /></button>
          <span className="ml-1 text-[9px] text-gray-600">{Math.round(view.scale * 100)}%</span>
        </div>
      </div>

      {/* Truncation / loading / error band */}
      {loading && <div className="shrink-0 border-b border-gray-800 bg-[#0d0d0d] px-3 py-1 text-[9px] text-amber-300" data-testid="gate-schematic-loading">loading bounded graph…</div>}
      {error && !loading && (
        <div className="shrink-0 border-b border-red-500/30 bg-[#140d0d] px-3 py-1 text-[9px] text-red-400" data-testid="gate-schematic-error">
          {error}
        </div>
      )}
      {graph?.truncated && !loading && (
        <div className="flex shrink-0 items-center gap-2 border-b border-amber-500/30 bg-[#141109] px-3 py-1 text-[9px] text-amber-300" data-testid="gate-schematic-truncated">
          <span>view truncated: {graph.truncated_reasons?.join('; ')} — showing {graph.counts?.nodes} of {graph.completeness?.cell_count ?? '?'} cells</span>
          {(scope?.limit ?? 0) < MAX_NODE_LIMIT && (
            <button type="button" onClick={() => setScope((current) => ({ ...current, limit: Math.min((current?.limit ?? DEFAULT_NODE_LIMIT) * 2, MAX_NODE_LIMIT) }))} className="rounded border border-amber-500/40 px-1.5 py-0.5 hover:bg-amber-500/10">
              raise node limit to {Math.min((scope?.limit ?? DEFAULT_NODE_LIMIT) * 2, MAX_NODE_LIMIT)}
            </button>
          )}
        </div>
      )}

      {/* Phase G3: ordered timing paths from the exact STA run, mapped onto
          this netlist. Setup/hold, corner, and clock are always visible. */}
      {timingPaths && timingPaths.available === false && (
        <div className="shrink-0 border-b border-gray-800 bg-[#0d0d0d] px-3 py-1 text-[9px] text-gray-600" data-testid="gate-schematic-timing-unavailable">
          timing paths unavailable — {timingPaths.reason}
        </div>
      )}
      {timingPaths?.paths?.length > 0 && (
        <div className="flex shrink-0 items-center gap-1.5 overflow-x-auto border-b border-gray-800 bg-[#0d0d0d] px-3 py-1 [scrollbar-width:thin]" data-testid="gate-schematic-timing">
          <span className="shrink-0 text-[8px] uppercase tracking-wider text-gray-600">timing paths</span>
          {timingPaths.paths.map((path, index) => (
            <button
              key={index}
              type="button"
              data-testid={`gate-schematic-path-${index}`}
              onClick={() => (selectedPathIndex === index ? clearPath() : selectPath(index))}
              className={`shrink-0 rounded border px-1.5 py-0.5 text-[9px] transition-colors ${
                selectedPathIndex === index
                  ? 'border-amber-500/60 bg-amber-500/10 text-amber-200'
                  : 'border-gray-800 text-gray-500 hover:border-gray-600 hover:text-gray-300'}`}
              title={`${path.analysis} path, corner ${path.corner ?? '?'}, clock ${path.clock ?? '?'}, slack ${path.slack_ns ?? '?'} ns`}
            >
              <span className={path.analysis === 'setup' ? 'text-cyan-300' : 'text-violet-300'}>{path.analysis}</span>
              <span className="mx-1 text-gray-600">{path.corner ?? '—'}</span>
              <span className="text-gray-500">{path.clock ?? '—'}</span>
              <span className={`ml-1 ${Number(path.slack_ns) < 0 ? 'text-red-400' : 'text-emerald-400'}`}>{path.slack_ns} ns</span>
              {path.mapping?.partial && <span className="ml-1 text-amber-400" title={`unresolved: ${path.mapping.unresolved.join(', ')}`}>partial</span>}
            </button>
          ))}
          {selectedPathIndex != null && (
            <button type="button" onClick={clearPath} className="shrink-0 rounded border border-gray-700 px-1.5 py-0.5 text-[9px] text-gray-400 hover:bg-white/5" data-testid="gate-schematic-clear-path">
              clear
            </button>
          )}
        </div>
      )}
      {pathMismatch && (
        <div className="shrink-0 border-b border-red-500/30 bg-[#140d0d] px-3 py-1 text-[9px] text-red-400" data-testid="gate-schematic-path-mismatch">
          {pathMismatch}
        </div>
      )}

      {/* Canvas + details */}
      <div className="flex min-h-0 flex-1">
        <div
          ref={containerRef}
          className="relative min-w-0 flex-1 overflow-hidden bg-[#0a0a0a]"
          tabIndex={0}
          onKeyDown={onKeyDown}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={onPointerUp}
          aria-label="Gate schematic canvas: arrow keys move the selection, plus and minus zoom, f fits"
        >
          {graph && layout && (graph.nodes?.length ?? 0) > 0 && (
            <svg ref={svgRef} className="absolute inset-0 h-full w-full touch-none select-none" role="img" aria-label={`Gate schematic for module ${graph.module}`}>
              <defs>
                <pattern id="schematic-dot-grid" width={24} height={24} patternUnits="userSpaceOnUse">
                  <circle cx={1} cy={1} r={0.8} fill="#1f1f27" />
                </pattern>
              </defs>
              <g transform={`translate(${view.x}, ${view.y}) scale(${view.scale})`} style={reduceMotion ? undefined : { transition: 'opacity 0.15s' }}>
                {layout.size && (
                  <rect
                    x={-200} y={-200}
                    width={layout.size.w + 400}
                    height={layout.size.h + 400}
                    fill="url(#schematic-dot-grid)"
                  />
                )}
                {routes.map((route, index) => {
                  const emphasized = emphasis ? emphasis.routes.has(route.net) : true
                  return (
                    <polyline
                      key={`${route.net}-${index}`}
                      points={route.points.map((point) => point.join(',')).join(' ')}
                      fill="none"
                      stroke={selection?.type === 'net' && selection.id === route.net ? '#fbbf24' : '#3f3f46'}
                      strokeWidth={emphasis ? (emphasized ? 1.4 : 0.6) : 1}
                      opacity={emphasis ? (emphasized ? 1 : 0.15) : 0.8}
                      strokeLinejoin="round"
                    />
                  )
                })}
                {(graph.nodes ?? []).map((node) => {
                  const position = positionById.get(node.id)
                  if (!position) return null
                  return (
                    <CellGlyph
                      key={node.id}
                      node={node}
                      pos={position}
                      selected={selection?.type === 'node' && selection.id === node.id}
                      dimmed={emphasis ? !emphasis.nodes.has(node.id) : false}
                      pathHighlighted={Boolean(pathHighlight?.nodes.has(node.id))}
                      onSelect={selectNode}
                      reduceMotion={reduceMotion}
                    />
                  )
                })}
              </g>
            </svg>
          )}
          {graph && (graph.nodes?.length ?? 0) === 0 && !loading && (
            <div className="absolute inset-0 flex items-center justify-center text-[10px] text-gray-600" data-testid="gate-schematic-empty">
              This scope renders no nodes — the selected module or cone has no visible logic within the node limit.
            </div>
          )}
          {minimap && layout && (graph?.nodes?.length ?? 0) > 0 && (
            <div className="absolute bottom-2 right-2 rounded border border-gray-800 bg-[#0d0d0d] p-1" data-testid="gate-schematic-minimap">
              <svg width={minimap.box.w} height={minimap.box.h} className="cursor-pointer" onClick={(event) => {
                const rect = event.currentTarget.getBoundingClientRect()
                const mx = (event.clientX - rect.left) / minimap.scale
                const my = (event.clientY - rect.top) / minimap.scale
                const container = containerRef.current
                if (!container) return
                const { width, height } = container.getBoundingClientRect()
                setView((current) => ({ ...current, x: width / 2 - mx * current.scale, y: height / 2 - my * current.scale }))
              }}>
                {layout.positions.map((position) => (
                  <rect key={position.id} x={position.x * minimap.scale} y={position.y * minimap.scale} width={Math.max(position.w * minimap.scale, 1.5)} height={Math.max(position.h * minimap.scale, 1)} fill="#525252" />
                ))}
                {viewportRect && (
                  <rect x={viewportRect.x} y={viewportRect.y} width={viewportRect.w} height={viewportRect.h} fill="rgba(52,211,153,0.08)" stroke="#34d399" strokeWidth={0.8} />
                )}
              </svg>
            </div>
          )}
          <div className="pointer-events-none absolute bottom-2 left-2 text-[8px] text-gray-700">
            {graph ? `${graph.counts?.nodes} nodes · ${graph.counts?.cells} cells · ${graph.counts?.nets} nets · limit ${scope?.limit}` : ''}
          </div>
        </div>

        {/* Selection details: exact properties and provenance */}
        <aside className="w-64 shrink-0 overflow-y-auto border-l border-gray-800 bg-[#0d0d0d] p-2.5" data-testid="gate-schematic-details">
          {selectedPath && (
            <div className="mb-2 rounded border border-amber-500/40 bg-[#141109] p-2" data-testid="gate-schematic-path-detail">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px]">
                <span className={selectedPath.analysis === 'setup' ? 'text-cyan-300' : 'text-violet-300'} data-testid="gate-schematic-path-analysis">{selectedPath.analysis}</span>
                <span className="text-gray-500">corner <b className="font-normal text-gray-300">{selectedPath.corner ?? '—'}</b></span>
                <span className="text-gray-500">clk <b className="font-normal text-gray-300">{selectedPath.clock ?? '—'}</b></span>
                <span className={Number(selectedPath.slack_ns) < 0 ? 'text-red-400' : 'text-emerald-400'}>{selectedPath.slack_ns} ns slack</span>
              </div>
              <div className="mt-1 text-[8px] text-gray-600">arrival {selectedPath.data_arrival_ns} · required {selectedPath.data_required_ns} ns</div>
              {selectedPath.mapping?.partial && (
                <div className="mt-1 text-[8px] text-amber-400" title={selectedPath.mapping.unresolved.join(', ')}>
                  partial mapping — {selectedPath.mapping.unresolved.length} unresolved segment(s)
                </div>
              )}
              <div className="mt-1.5 space-y-0.5">
                {(selectedPath.segments ?? []).filter((segment) => segment.kind !== 'clock').map((segment, index) => (
                  <button
                    key={index}
                    type="button"
                    className="flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left text-[9px] hover:bg-white/5"
                    onClick={() => {
                      if (!segment.graph_id) return
                      if (segment.graph_id.startsWith('net:')) selectNet(segment.graph_id)
                      else selectNode(nodeById.get(segment.graph_id))
                    }}
                    title={segment.graph_id ? `select ${segment.graph_id}` : 'unresolved segment'}
                  >
                    <span className="w-6 shrink-0 text-gray-700">{segment.kind === 'net' ? 'net' : segment.kind === 'port' ? 'port' : 'pin'}</span>
                    <span className="min-w-0 flex-1 truncate" style={{ color: segment.graph_id ? '#d4d4d4' : '#92400e' }}>{segment.name}</span>
                    <span className="shrink-0 text-gray-500">{segment.delay_ns ?? '—'}</span>
                    <span className="w-10 shrink-0 text-right text-gray-400">{segment.time_ns ?? '—'}</span>
                  </button>
                ))}
              </div>
            </div>
          )}
          {selectedNode && (
            <div className="space-y-2">
              <div className="text-[11px] text-gray-200">{selectedNode.name}</div>
              <div className="space-y-1 text-[9px] text-gray-500">
                {selectedNode.type && <div>type <span className="text-gray-300">{selectedNode.type}</span></div>}
                <div>module <span className="text-gray-300">{selectedNode.module}</span></div>
                {selectedNode.hierarchy && <div>hierarchy <span className="text-violet-400">{selectedNode.hierarchy}</span></div>}
                {selectedNode.black_box && <div className="text-orange-400">black-box module</div>}
                {!selectedNode.directions_known && selectedNode.kind === 'cell' && <div className="text-amber-400">pin directions unknown — edges omitted</div>}
                {selectedNode.source && (
                  <button type="button" className="flex items-center gap-1 text-cyan-400 hover:text-cyan-300" onClick={() => onOpenSource?.(sourceOfSelection)} title={`Open ${selectedNode.source} in Sources`}>
                    <Crosshair className="h-3 w-3" /> {selectedNode.source}
                  </button>
                )}
              </div>
              {Object.keys(selectedNode.pins ?? {}).length > 0 && (
                <div>
                  <div className="mb-1 text-[8px] uppercase tracking-wider text-gray-600">pins</div>
                  {Object.entries(selectedNode.pins).map(([pin, nets]) => (
                    <div key={pin} className="flex items-center gap-1 text-[9px]">
                      <span className="w-14 shrink-0 truncate text-gray-500" title={pin}>{pin}</span>
                      <span className="text-gray-700">{selectedNode.pin_directions?.[pin] ?? '?'}</span>
                      <button type="button" className="truncate text-cyan-400/80 hover:text-cyan-300" onClick={() => selectNet(`net:${selectedNode.module}:${nets[0]}`)} title={`Select net ${nets[0]}`}>
                        {nets[0]}
                      </button>
                    </div>
                  ))}
                </div>
              )}
              {selectedNode.hierarchy && (
                <button type="button" onClick={() => openScope(selectedNode.hierarchy, `${selectedNode.name} (${selectedNode.hierarchy})`)} className="w-full rounded border border-violet-500/40 px-2 py-1 text-[9px] text-violet-300 hover:bg-violet-500/10" data-testid="gate-schematic-expand">
                  expand {selectedNode.hierarchy}
                </button>
              )}
              {selectedNode.kind === 'cell' && (
                <button type="button" onClick={() => openCone(selectedNode.id)} className="w-full rounded border border-gray-700 px-2 py-1 text-[9px] text-gray-400 hover:bg-white/5" data-testid="gate-schematic-cone-button">
                  restrict to fan-in / fan-out cone
                </button>
              )}
            </div>
          )}
          {selectedNet && (
            <div className="space-y-2">
              <div className="text-[11px] text-gray-200">{selectedNet.name}</div>
              <div className="space-y-1 text-[9px] text-gray-500">
                <div>net <span className="text-gray-300">{selectedNet.id}</span></div>
                {selectedNet.names?.length > 1 && <div className="truncate" title={selectedNet.names.join(', ')}>aliases <span className="text-gray-300">{selectedNet.names.join(', ')}</span></div>}
                {selectedNet.source && (
                  <button type="button" className="flex items-center gap-1 text-cyan-400 hover:text-cyan-300" onClick={() => onOpenSource?.(parseSourceAttr(selectedNet.source))}>
                    <Crosshair className="h-3 w-3" /> {selectedNet.source}
                  </button>
                )}
                <div>drivers: {(selectedNet.drivers ?? []).map((pin) => pin.node).join(', ') || '—'}</div>
                <div>loads: {(selectedNet.loads ?? []).map((pin) => pin.node).join(', ') || '—'}</div>
                <div>fanout <span className="text-gray-300">{(selectedNet.loads ?? []).length}</span></div>
              </div>
            </div>
          )}
          {!selection && (
            <div className="text-[10px] leading-relaxed text-gray-600">
              Select a cell, port, or net in the schematic to inspect its exact properties, connections, and source provenance.
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}
