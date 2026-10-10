import { useCallback, useEffect, useMemo, useState } from 'react'
import GateSchematicCanvas from './gate-schematic/GateSchematicCanvas'
import GateSchematicDetails from './gate-schematic/GateSchematicDetails'
import GateSchematicTiming from './gate-schematic/GateSchematicTiming'
import GateSchematicToolbar from './gate-schematic/GateSchematicToolbar'
import { useGateSchematicData } from './gate-schematic/useGateSchematicData'
import { useGateViewport } from './gate-schematic/useGateViewport'
import { DEFAULT_NODE_LIMIT, MAX_NODE_LIMIT, MIN_SCALE, MAX_SCALE, parseSourceAttr, useReducedMotion } from './gate-schematic/utils'

// Bounded gate-level schematic (Phase G2). Everything rendered comes from the
// canonical gate-netlist artifact served by the bounded graph endpoint; when
// the artifact is absent the panel says so instead of inventing a diagram.

export default function GateSchematic({ job, onOpenSource, onSelectionChange, onPathChange, externalSelection }) {
  const reduceMotion = useReducedMotion()
  const jobId = job?.id ?? null
  const data = useGateSchematicData(jobId)
  const { meta, metaError, scope, setScope, breadcrumb, setBreadcrumb, graph, layout, loading, error,
    timingPaths, selectedPathIndex, pathMismatch, setSelectedPathIndex, setPathMismatch } = data
  const [selection, setSelection] = useState(null) // { type: 'node'|'net', id }
  const [search, setSearch] = useState('')

  // A job change invalidates the selection before a new evidence-bound graph arrives.
  // oxlint-disable-next-line react/set-state-in-effect
  useEffect(() => { setSelection(null) }, [jobId])

  const nodeById = useMemo(() => new Map((graph?.nodes ?? []).map((node) => [node.id, node])), [graph])
  const netById = useMemo(() => new Map((graph?.nets ?? []).map((net) => [net.id, net])), [graph])
  const positionById = useMemo(() => new Map((layout?.positions ?? []).map((item) => [item.id, item])), [layout])
  const routes = useMemo(() => layout?.routes ?? [], [layout])

  const selectNodeForKeyboard = useCallback((node) => {
    if (node) setSelection({ type: 'node', id: node.id })
  }, [])
  const viewport = useGateViewport({ layout, positionById, nodeById, selection, selectNode: selectNodeForKeyboard })
  const { containerRef, svgRef, view, setView, fit, centerOn, onPointerDown, onPointerMove,
    onPointerUp, onKeyDown, minimap, viewportRect, onMinimapClick } = viewport
  const selectNode = useCallback((node) => {
    if (!node) return
    selectNodeForKeyboard(node)
    const position = positionById.get(node.id)
    if (position) centerOn(position)
  }, [selectNodeForKeyboard, positionById, centerOn])

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
      <GateSchematicToolbar
        meta={meta} breadcrumb={breadcrumb} graph={graph} scope={scope}
        search={search} setSearch={setSearch} searchResults={searchResults}
        openScope={openScope} goBack={goBack} clearCone={clearCone}
        selectNode={(id) => selectNode(nodeById.get(id))} selectNet={selectNet}
        setView={setView} view={view} fit={fit} minScale={MIN_SCALE} maxScale={MAX_SCALE}
      />

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

      <GateSchematicTiming
        timingPaths={timingPaths} selectedPathIndex={selectedPathIndex}
        selectPath={selectPath} clearPath={clearPath} pathMismatch={pathMismatch}
      />

      <div className="flex min-h-0 flex-1">
        <GateSchematicCanvas
          containerRef={containerRef} svgRef={svgRef} graph={graph} layout={layout}
          loading={loading} scope={scope} view={view} reduceMotion={reduceMotion}
          routes={routes} emphasis={emphasis} selection={selection} pathHighlight={pathHighlight}
          selectNode={selectNode} onKeyDown={onKeyDown} onPointerDown={onPointerDown}
          onPointerMove={onPointerMove} onPointerUp={onPointerUp} minimap={minimap}
          viewportRect={viewportRect} onMinimapClick={onMinimapClick}
        />
        <GateSchematicDetails
          selectedPath={selectedPath} selectedNode={selectedNode} selectedNet={selectedNet}
          selection={selection} nodeById={nodeById} onSelectNode={selectNode}
          onSelectNet={selectNet} onOpenSource={onOpenSource} sourceOfSelection={sourceOfSelection}
          openScope={openScope} openCone={openCone}
        />
      </div>
    </div>
  )
}
