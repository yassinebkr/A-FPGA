import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { MAX_SCALE, MIN_SCALE } from './utils'

// Encapsulates canvas geometry and gestures. Graph fetching and selection
// semantics remain outside this hook; it only translates user input into a
// viewport and delegates keyboard selection to the root.
export function useGateViewport({ layout, positionById, nodeById, selection, selectNode }) {
  const containerRef = useRef(null)
  const svgRef = useRef(null)
  const dragRef = useRef(null)
  const [containerSize, setContainerSize] = useState(null)
  const [view, setView] = useState({ x: 0, y: 0, scale: 1 })

  useEffect(() => {
    const container = containerRef.current
    if (!container) return undefined
    const update = () => setContainerSize({ w: container.clientWidth, h: container.clientHeight })
    update()
    const observer = new ResizeObserver(update)
    observer.observe(container)
    return () => observer.disconnect()
  }, [])

  const fit = useCallback(() => {
    if (!containerSize?.w || !containerSize?.h || !layout?.size) return
    const scale = Math.min(Math.max(Math.min(containerSize.w / layout.size.w, containerSize.h / layout.size.h) * 0.92, MIN_SCALE), MAX_SCALE)
    setView({ scale, x: (containerSize.w - layout.size.w * scale) / 2, y: (containerSize.h - layout.size.h * scale) / 2 })
  }, [layout, containerSize])

  const fittedForRef = useRef(null)
  const prevSizeRef = useRef(null)
  useEffect(() => {
    const wasHidden = prevSizeRef.current != null && (prevSizeRef.current.w === 0 || prevSizeRef.current.h === 0)
    prevSizeRef.current = containerSize
    const visible = containerSize && containerSize.w > 0 && containerSize.h > 0
    if (layout && visible && (fittedForRef.current !== layout || wasHidden)) {
      fittedForRef.current = layout
      fit()
    }
  }, [layout, containerSize, fit])

  const centerOn = useCallback((position) => {
    if (!containerSize || !position) return
    setView((current) => ({ ...current, x: containerSize.w / 2 - (position.x + position.w / 2) * current.scale, y: containerSize.h / 2 - (position.y + position.h / 2) * current.scale }))
  }, [containerSize])

  const onPointerDown = useCallback((event) => {
    if (event.button !== 0 || event.target.closest?.('[role="button"]')) return
    dragRef.current = { startX: event.clientX, startY: event.clientY, viewX: view.x, viewY: view.y }
    event.currentTarget.setPointerCapture(event.pointerId)
  }, [view.x, view.y])
  const onPointerMove = useCallback((event) => {
    const drag = dragRef.current
    if (!drag) return
    setView((current) => ({ ...current, x: drag.viewX + (event.clientX - drag.startX), y: drag.viewY + (event.clientY - drag.startY) }))
  }, [])
  const onPointerUp = useCallback((event) => {
    dragRef.current = null
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }, [])
  const onWheel = useCallback((event) => {
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
  }, [])
  useEffect(() => {
    const container = containerRef.current
    if (!container) return undefined
    container.addEventListener('wheel', onWheel, { passive: false })
    return () => container.removeEventListener('wheel', onWheel)
  }, [onWheel])

  const onKeyDown = useCallback((event) => {
    if (!positionById.size) return
    if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', '+', '-', 'f'].includes(event.key)) return
    if (event.key === 'f') { fit(); return }
    if (event.key === '+') { setView((current) => ({ ...current, scale: Math.min(current.scale * 1.2, MAX_SCALE) })); return }
    if (event.key === '-') { setView((current) => ({ ...current, scale: Math.max(current.scale / 1.2, MIN_SCALE) })); return }
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
    if (best) {
      selectNode(nodeById.get(best))
      centerOn(positionById.get(best))
    }
  }, [positionById, selection, fit, nodeById, selectNode, centerOn])

  const minimap = useMemo(() => {
    if (!layout?.size) return null
    const box = { w: 150, h: 96 }
    return { box, scale: Math.min(box.w / layout.size.w, box.h / layout.size.h) }
  }, [layout])
  const viewportRect = useMemo(() => {
    if (!minimap || !containerSize) return null
    return { x: (-view.x / view.scale) * minimap.scale, y: (-view.y / view.scale) * minimap.scale, w: (containerSize.w / view.scale) * minimap.scale, h: (containerSize.h / view.scale) * minimap.scale }
  }, [view, minimap, containerSize])
  const onMinimapClick = useCallback((event) => {
    if (!minimap) return
    const rect = event.currentTarget.getBoundingClientRect()
    const mx = (event.clientX - rect.left) / minimap.scale
    const my = (event.clientY - rect.top) / minimap.scale
    const container = containerRef.current
    if (!container) return
    const { width, height } = container.getBoundingClientRect()
    setView((current) => ({ ...current, x: width / 2 - mx * current.scale, y: height / 2 - my * current.scale }))
  }, [minimap])

  return { containerRef, svgRef, containerSize, view, setView, fit, centerOn, onPointerDown, onPointerMove, onPointerUp, onKeyDown, minimap, viewportRect, onMinimapClick }
}
