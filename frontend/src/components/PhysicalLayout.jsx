import { useCallback, useEffect, useRef, useState } from 'react'
import { Maximize2, RotateCcw, ZoomIn, ZoomOut } from 'lucide-react'
import { fetchJobLayout } from '../hooks/useAgentSocket'

// Phase G4: the post-P&R companion view. Everything rendered comes from the
// indexed, digest-bound layout-view artifact derived from the routed DEF;
// a job without a physical implementation shows an honest unavailable state.
// Cell footprints are nominal (DEF carries placement, not LEF dimensions).

const MIN_SCALE = 0.02
const MAX_SCALE = 40

// The canvas mounts after the loading/unavailable early returns, so the
// observer attaches through a callback ref instead of running once at mount.
function useContainerSize() {
  const [node, setNode] = useState(null)
  const [size, setSize] = useState(null)
  useEffect(() => {
    if (!node) return undefined
    const update = () => setSize({ w: node.clientWidth, h: node.clientHeight })
    update()
    const observer = new ResizeObserver(update)
    observer.observe(node)
    return () => observer.disconnect()
  }, [node])
  return [setNode, size]
}

export default function PhysicalLayout({ job, selection, pathCells, onSelectComponent, onAvailability }) {
  const containerRef = useRef(null)
  const [containerNodeSetter, containerSize] = useContainerSize()
  const [view, setView] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 })
  const dragRef = useRef(null)

  const jobId = job?.id ?? null

  useEffect(() => {
    if (!jobId) {
      // A job boundary invalidates the async view before the next request.
      // oxlint-disable-next-line react/set-state-in-effect
      setView(null)
      setError(null)
      return undefined
    }
    let cancelled = false
    // This effect synchronizes the view with the selected job async request.
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    setView(null)
    setError(null)
    fetchJobLayout(jobId).then((result) => {
      if (cancelled) return
      setLoading(false)
      if (!result.ok) {
        setError(result.error)
        return
      }
      const payload = result.data
      setView(payload)
      onAvailability?.(payload)
    })
    return () => { cancelled = true }
  }, [jobId, onAvailability])

  const attachContainer = useCallback((node) => {
    containerRef.current = node
    containerNodeSetter(node)
  }, [containerNodeSetter])
  const die = view?.die
  const fit = useCallback(() => {
    // A hidden container reports zero size; fitting against it would produce
    // a garbage transform. Refit happens when the view becomes visible.
    if (!containerSize || !containerSize.w || !containerSize.h || !die) return
    const scale = Math.min(
      Math.max(Math.min(containerSize.w / (die.urx - die.llx), containerSize.h / (die.ury - die.lly)) * 0.9, MIN_SCALE),
      MAX_SCALE)
    setTransform({
      scale,
      x: (containerSize.w - (die.urx - die.llx) * scale) / 2,
      y: (containerSize.h - (die.ury - die.lly) * scale) / 2,
    })
  }, [containerSize, die])

  // Fit each new view once; because the layout stays mounted while the
  // schematic subview is active, a fit computed against a zero-size (hidden)
  // canvas is repeated as soon as the surface becomes visible.
  const fittedRef = useRef(null)
  const prevSizeRef = useRef(null)
  useEffect(() => {
    const wasHidden = prevSizeRef.current != null
      && (prevSizeRef.current.w === 0 || prevSizeRef.current.h === 0)
    prevSizeRef.current = containerSize
    const visible = containerSize && containerSize.w > 0 && containerSize.h > 0
    if (view && view.die && visible && (fittedRef.current !== view || wasHidden)) {
      fittedRef.current = view
      fit()
    }
  }, [view, containerSize, fit])

  // The schematic selection locates the same instance in the physical view.
  const selectedId = selection?.type === 'node' ? selection.id : null
  const selectedComponent = view?.components?.find((component) => component.graph_id === selectedId) ?? null
  const pathComponentNames = new Set((pathCells ?? []).map((name) => `cell:${name}`))
  const pathComponents = view?.components?.filter((component) =>
    pathComponentNames.has(component.graph_id)) ?? []

  // Center on the selected instance when the selection changes from the
  // schematic side (cross-probing works in both directions).
  const centeredRef = useRef(null)
  useEffect(() => {
    if (!selectedComponent || !containerSize) return
    if (centeredRef.current === selectedComponent.name) return
    centeredRef.current = selectedComponent.name
    setTransform((current) => ({
      ...current,
      x: containerSize.w / 2 - selectedComponent.x * current.scale,
      y: containerSize.h / 2 - selectedComponent.y * current.scale,
    }))
  }, [selectedComponent, containerSize])

  const onPointerDown = (event) => {
    if (event.button !== 0) return
    if (event.target.closest?.('[role="button"]')) return
    dragRef.current = { startX: event.clientX, startY: event.clientY, x: transform.x, y: transform.y }
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const onPointerMove = (event) => {
    const drag = dragRef.current
    if (!drag) return
    setTransform((current) => ({
      ...current,
      x: drag.x + (event.clientX - drag.startX),
      y: drag.y + (event.clientY - drag.startY),
    }))
  }
  const onPointerUp = (event) => {
    dragRef.current = null
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }
  useEffect(() => {
    const element = containerRef.current
    if (!element) return undefined
    const handler = (event) => {
      event.preventDefault()
      const rect = element.getBoundingClientRect()
      const mx = event.clientX - rect.left
      const my = event.clientY - rect.top
      setTransform((current) => {
        const factor = Math.exp(-event.deltaY * 0.0015)
        const scale = Math.min(Math.max(current.scale * factor, MIN_SCALE), MAX_SCALE)
        const ratio = scale / current.scale
        return { scale, x: mx - (mx - current.x) * ratio, y: my - (my - current.y) * ratio }
      })
    }
    element.addEventListener('wheel', handler, { passive: false })
    return () => element.removeEventListener('wheel', handler)
  })

  if (!jobId) {
    return <div className="flex h-full items-center justify-center font-mono text-[10px] text-gray-600" data-testid="physical-layout">no job selected</div>
  }
  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1 px-6 text-center font-mono" data-testid="physical-layout">
        <div className="text-sm text-red-400">layout unavailable</div>
        <div className="max-w-md text-[10px] text-gray-600">{error}</div>
      </div>
    )
  }
  if (view && view.available === false) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1 px-6 text-center font-mono" data-testid="physical-layout">
        <div className="text-sm text-gray-500" data-testid="physical-layout-unavailable">no physical implementation for this job</div>
        <div className="max-w-md text-[10px] leading-relaxed text-gray-700" data-testid="physical-layout-reason">{view.reason}</div>
        <div className="max-w-md text-[10px] leading-relaxed text-gray-700">Only a post-P&amp;R run with an emitted DEF produces the layout view; nothing is fabricated from synthesis-only evidence.</div>
      </div>
    )
  }
  if (!view || loading) {
    return <div className="flex h-full items-center justify-center font-mono text-[10px] text-gray-600" data-testid="physical-layout">loading layout view…</div>
  }

  const footprint = view.nominal_footprint ?? { w: 460, h: 2720 }
  const [ox, oy] = [die.llx, die.lly]
  const dieW = die.urx - die.llx
  const dieH = die.ury - die.lly
  // Zoom-adaptive rendering: far out, cells collapse to dots; close in, they
  // become (nominal) rectangles with a type label.
  const cellLevel = transform.scale > 0.25

  return (
    <div className="flex h-full min-h-0 flex-col font-mono" data-testid="physical-layout">
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-gray-800 bg-[#0f0f0f] px-3 py-1.5 text-[9px] text-gray-500">
        <span className="uppercase tracking-wider text-gray-400">physical layout</span>
        <span>design <b className="font-normal text-gray-300">{view.identity?.design ?? '—'}</b></span>
        <span title={view.identity?.def_sha256}>def {String(view.identity?.def_sha256 ?? '').slice(0, 10)}</span>
        {view.identity?.netlist_sha256 && <span title={view.identity.netlist_sha256}>netlist {view.identity.netlist_sha256.slice(0, 10)}</span>}
        <span>die {(dieW / (view.units || 1000)).toFixed(1)} x {(dieH / (view.units || 1000)).toFixed(1)} um</span>
        <span>{view.completeness?.component_count} placed cells</span>
        {view.completeness?.nominal_cell_footprint && (
          <span className="text-gray-600" title="DEF placement carries coordinates, not LEF dimensions; rectangles are nominal site footprints.">nominal footprints</span>
        )}
        {view.completeness?.mapping_partial && (
          <span className="text-amber-400" title="Some placed cells do not exist in the bound netlist.">partial netlist mapping</span>
        )}
        {view.completeness?.truncated && <span className="text-amber-400">view truncated</span>}
        <div className="ml-auto flex items-center gap-1">
          <button type="button" onClick={() => setTransform((t) => ({ ...t, scale: Math.min(t.scale * 1.4, MAX_SCALE) }))} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Zoom in" aria-label="Zoom in"><ZoomIn className="h-3 w-3" /></button>
          <button type="button" onClick={() => setTransform((t) => ({ ...t, scale: Math.max(t.scale / 1.4, MIN_SCALE) }))} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Zoom out" aria-label="Zoom out"><ZoomOut className="h-3 w-3" /></button>
          <button type="button" onClick={fit} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Fit to view" aria-label="Fit to view"><Maximize2 className="h-3 w-3" /></button>
          <button type="button" onClick={() => { setTransform({ x: 0, y: 0, scale: 1 }); fit() }} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Reset view" aria-label="Reset view"><RotateCcw className="h-3 w-3" /></button>
        </div>
      </div>
      <div
        ref={attachContainer}
        className="relative min-h-0 flex-1 overflow-hidden bg-[#0a0a0a]"
        tabIndex={0}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerUp}
        aria-label="Physical layout canvas: drag to pan, wheel to zoom"
      >
        <svg className="absolute inset-0 h-full w-full touch-none select-none" role="img" aria-label={`Physical layout for ${view.identity?.design ?? 'design'}`}>
          <g transform={`translate(${transform.x}, ${transform.y}) scale(${transform.scale})`}>
            {/* Die and core */}
            <rect x={ox} y={oy} width={dieW} height={dieH} fill="#0d0d0d" stroke="#525252" strokeWidth={4 / transform.scale} />
            {/* Rows */}
            {view.rows?.map((row, index) => (
              <rect key={`row-${index}`} x={row.x} y={row.y}
                width={row.num_x * row.step_x} height={row.num_y * row.step_y || footprint.h}
                fill="rgba(82,82,82,0.10)" stroke="none" />
            ))}
            {/* Placed cells; far out they collapse to dots */}
            {view.components.map((component) => {
              const isSelected = selectedComponent?.name === component.name
              const onPath = pathComponents.some((item) => item.name === component.name)
              const width = cellLevel ? footprint.w : 4 / transform.scale
              const height = cellLevel ? footprint.h : 4 / transform.scale
              return (
                <rect
                  key={component.name}
                  x={component.x} y={component.y}
                  width={width} height={height}
                  fill={isSelected ? '#34d399' : onPath ? '#fbbf24' : '#3f3f46'}
                  opacity={isSelected || onPath ? 1 : 0.85}
                  stroke={isSelected ? '#a7f3d0' : onPath ? '#fde68a' : 'none'}
                  strokeWidth={(isSelected || onPath ? 6 : 1) / transform.scale}
                  role="button"
                  tabIndex={0}
                  aria-label={`placed cell ${component.name}, ${component.macro}`}
                  onClick={(event) => {
                    event.stopPropagation()
                    if (component.graph_id) onSelectComponent?.(component.graph_id)
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      if (component.graph_id) onSelectComponent?.(component.graph_id)
                    }
                  }}
                  className="cursor-pointer focus:outline-none"
                />
              )
            })}
            {/* IO pins */}
            {view.pins?.filter((pin) => pin.x != null).map((pin) => (
              <g key={`pin-${pin.name}`}>
                <circle cx={pin.x} cy={pin.y} r={8 / transform.scale} fill={pin.direction === 'INPUT' ? '#8b9dc3' : '#c3a78b'} />
                {cellLevel && (
                  <text x={pin.x + 12 / transform.scale} y={pin.y} fontSize={10 / transform.scale} fill="#8b9dc3" fontFamily="ui-monospace, monospace">{pin.name}</text>
                )}
              </g>
            ))}
          </g>
        </svg>
        <div className="pointer-events-none absolute bottom-2 left-2 text-[8px] text-gray-700">
          {transform.scale > 0.25 ? 'cell level' : 'block level — zoom in for cells'}
        </div>
      </div>
    </div>
  )
}
