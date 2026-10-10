import { useCallback, useEffect, useRef, useState } from 'react'

export const DEFAULT_NODE_LIMIT = 500
export const MAX_NODE_LIMIT = 2000
export const MIN_SCALE = 0.08
export const MAX_SCALE = 4

export function parseSourceAttr(source) {
  if (!source) return null
  const index = source.lastIndexOf(':')
  if (index <= 0) return { file: source, line: null }
  const line = Number(source.slice(index + 1))
  return Number.isInteger(line) && line > 0
    ? { file: source.slice(0, index), line }
    : { file: source, line: null }
}

export function shortDigest(digest) {
  return typeof digest === 'string' ? digest.slice(0, 10) : '—'
}

export function pinSide(node, pin) {
  const direction = node.pin_directions?.[pin]
  if (direction === 'input') return 'left'
  if (direction === 'output') return 'right'
  return null
}

export function useReducedMotion() {
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

// Layout stays off the main thread. The hook owns worker startup, request
// correlation, and teardown so rendering components do not know its protocol.
export function useLayoutWorker() {
  const workerRef = useRef(null)
  const pendingRef = useRef(new Map())
  useEffect(() => {
    const worker = new Worker(new URL('../../workers/graphLayout.worker.js', import.meta.url), { type: 'module' })
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
    const pending = pendingRef.current
    return () => {
      worker.terminate()
      workerRef.current = null
      for (const request of pending.values()) request.reject(new Error('layout worker stopped'))
      pending.clear()
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
