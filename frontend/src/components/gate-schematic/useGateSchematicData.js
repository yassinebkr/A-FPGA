import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchJobNetlist, fetchJobNetlistGraph, fetchJobTimingPaths } from '../../hooks/useAgentSocket'
import { DEFAULT_NODE_LIMIT, useLayoutWorker } from './utils'

// Owns evidence-bound fetches and their cancellation. The root component gets
// plain state plus setters, while late responses are rejected at this boundary.
export function useGateSchematicData(jobId) {
  const layoutGraph = useLayoutWorker()
  const [meta, setMeta] = useState(null)
  const [metaError, setMetaError] = useState(null)
  const [scope, setScope] = useState(null)
  const [breadcrumb, setBreadcrumb] = useState([])
  const [graph, setGraph] = useState(null)
  const [layout, setLayout] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [timingPaths, setTimingPaths] = useState(null)
  const [selectedPathIndex, setSelectedPathIndex] = useState(null)
  const [pathMismatch, setPathMismatch] = useState(null)
  const graphRequestIdRef = useRef(0)

  const resetForJob = useCallback((summary) => {
    setScope(null)
    setBreadcrumb([])
    setGraph(null)
    setLayout(null)
    setError(null)
    if (summary?.available) {
      setScope({ module: summary.top_module, limit: DEFAULT_NODE_LIMIT, cone: null })
      setBreadcrumb([{ module: summary.top_module, label: summary.top_module }])
    }
  }, [])

  useEffect(() => {
    if (!jobId) {
      // Job changes intentionally clear evidence-bound state.
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

  useEffect(() => {
    if (!jobId || !meta?.available || !scope) return undefined
    const requestId = graphRequestIdRef.current + 1
    graphRequestIdRef.current = requestId
    let cancelled = false
    // The graph request owns these loading/error transitions.
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

  return {
    meta, setMeta, metaError,
    scope, setScope, breadcrumb, setBreadcrumb,
    graph, layout, loading, error,
    timingPaths, setTimingPaths,
    selectedPathIndex, setSelectedPathIndex,
    pathMismatch, setPathMismatch,
  }
}
