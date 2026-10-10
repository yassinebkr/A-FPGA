import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchJobLayout } from './useAgentSocket'
import { fitTransform, MAX_SCALE, MIN_SCALE, zoomTransform } from '../components/physicalLayoutModel'

export function usePhysicalLayoutData(jobId, onAvailability) {
  const [state, setState] = useState({ jobId: null, view: null, error: null, loading: false })
  const availabilityRef = useRef(onAvailability)

  useEffect(() => {
    availabilityRef.current = onAvailability
  }, [onAvailability])

  useEffect(() => {
    let cancelled = false
    if (!jobId) {
      // The effect synchronizes asynchronous job-scoped data with the selected job.
      // oxlint-disable-next-line react/set-state-in-effect
      setState({ jobId: null, view: null, error: null, loading: false })
      return () => { cancelled = true }
    }

    // oxlint-disable-next-line react/set-state-in-effect
    setState({ jobId, view: null, error: null, loading: true })
    fetchJobLayout(jobId).then((result) => {
      if (cancelled) return
      if (!result.ok) {
        setState({ jobId, view: null, error: result.error, loading: false })
        return
      }
      setState({ jobId, view: result.data, error: null, loading: false })
      availabilityRef.current?.(result.data)
    }).catch((requestError) => {
      if (cancelled) return
      setState({
        jobId,
        view: null,
        error: requestError instanceof Error ? requestError.message : String(requestError),
        loading: false,
      })
    })
    return () => { cancelled = true }
  }, [jobId])

  return state.jobId === jobId ? state : { jobId, view: null, error: null, loading: Boolean(jobId) }
}

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

  return { node, setNode, size }
}

export function usePhysicalLayoutViewport({ view, selectedComponent }) {
  const { node: containerNode, setNode: setContainerNode, size: containerSize } = useContainerSize()
  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 })
  const dragRef = useRef(null)
  const fittedRef = useRef(null)
  const prevSizeRef = useRef(null)
  const centeredRef = useRef(null)
  const die = view?.die

  const attachContainer = useCallback((node) => {
    setContainerNode(node)
  }, [setContainerNode])

  const fit = useCallback(() => {
    const next = fitTransform(containerSize, die)
    if (next) setTransform(next)
  }, [containerSize, die])

  useEffect(() => {
    const wasHidden = prevSizeRef.current != null
      && (prevSizeRef.current.w === 0 || prevSizeRef.current.h === 0)
    prevSizeRef.current = containerSize
    const visible = containerSize && containerSize.w > 0 && containerSize.h > 0
    if (view && die && visible && (fittedRef.current !== view || wasHidden)) {
      fittedRef.current = view
      fit()
    }
  }, [view, die, containerSize, fit])

  useEffect(() => {
    if (!view) {
      centeredRef.current = null
      return
    }
    if (!selectedComponent || !containerSize) return
    if (centeredRef.current === selectedComponent.name) return
    centeredRef.current = selectedComponent.name
    // Selection changes are synchronized with the canvas viewport.
    // oxlint-disable-next-line react/set-state-in-effect
    setTransform((current) => ({
      ...current,
      x: containerSize.w / 2 - selectedComponent.x * current.scale,
      y: containerSize.h / 2 - selectedComponent.y * current.scale,
    }))
  }, [view, selectedComponent, containerSize])

  useEffect(() => {
    const element = containerNode
    if (!element) return undefined
    const handler = (event) => {
      event.preventDefault()
      const rect = element.getBoundingClientRect()
      const point = { x: event.clientX - rect.left, y: event.clientY - rect.top }
      setTransform((current) => zoomTransform(current, Math.exp(-event.deltaY * 0.0015), point))
    }
    element.addEventListener('wheel', handler, { passive: false })
    return () => element.removeEventListener('wheel', handler)
  }, [containerNode])

  const onPointerDown = useCallback((event) => {
    if (event.button !== 0) return
    if (event.target.closest?.('[role="button"]')) return
    dragRef.current = { startX: event.clientX, startY: event.clientY, x: transform.x, y: transform.y }
    event.currentTarget.setPointerCapture(event.pointerId)
  }, [transform.x, transform.y])

  const onPointerMove = useCallback((event) => {
    const drag = dragRef.current
    if (!drag) return
    setTransform((current) => ({
      ...current,
      x: drag.x + (event.clientX - drag.startX),
      y: drag.y + (event.clientY - drag.startY),
    }))
  }, [])

  const onPointerUp = useCallback((event) => {
    dragRef.current = null
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }, [])

  const zoomIn = useCallback(() => {
    setTransform((current) => ({ ...current, scale: Math.min(current.scale * 1.4, MAX_SCALE) }))
  }, [])
  const zoomOut = useCallback(() => {
    setTransform((current) => ({ ...current, scale: Math.max(current.scale / 1.4, MIN_SCALE) }))
  }, [])
  const reset = useCallback(() => {
    setTransform({ x: 0, y: 0, scale: 1 })
    fit()
  }, [fit])

  return {
    attachContainer,
    fit,
    onPointerDown,
    onPointerMove,
    onPointerUp,
    reset,
    transform,
    zoomIn,
    zoomOut,
    cellLevel: transform.scale > 0.25,
  }
}
