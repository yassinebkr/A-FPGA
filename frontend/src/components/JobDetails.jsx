import { useEffect, useState } from 'react'
import { extractMetricSnapshot } from '../hooks/useAgentSocket'

const API_URL = `http://${window.location.hostname || 'localhost'}:${import.meta.env.VITE_BACKEND_PORT || '8000'}`

function Field({ label, children }) {
  if (children === null || children === undefined || children === '') return null
  return <div><dt className="text-[9px] uppercase tracking-wider text-gray-600">{label}</dt><dd className="text-[10px] text-gray-300 break-all">{children}</dd></div>
}

// Result-first summary of what the job actually produced. The outcome banner
// leads; measured metrics (when the manifest carries them) and the failure
// text follow; the raw execution plumbing stays available, collapsed.
export default function JobDetails({ job }) {
  const [detail, setDetail] = useState(null)
  const [state, setState] = useState('idle')
  useEffect(() => {
    if (!job?.id) return
    const controller = new AbortController()
    fetch(`${API_URL}/api/jobs/${encodeURIComponent(job.id)}`, { signal: controller.signal })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
      .then((value) => { setDetail(value); setState('ready') })
      .catch((error) => { if (error.name !== 'AbortError') setState('unavailable') })
    return () => controller.abort()
  }, [job?.id, job?.status])
  const value = detail ?? job
  const evidence = value?.evidence
  const manifest = value?.manifest ?? job?.manifest
  const runtime = value?.started_at && value?.finished_at ? `${Math.max(0, (Date.parse(value.finished_at) - Date.parse(value.started_at)) / 1000).toFixed(1)} s` : null
  const outcome = value?.outcome ?? null
  const failed = outcome === 'failure' || value?.status === 'failed'
  const inconclusive = outcome === 'inconclusive'
  // Same extraction as the dashboard cards: the manifest may be the metrics
  // dict itself or carry a `metrics` sub-object.
  const metrics = manifest && typeof manifest === 'object'
    ? extractMetricSnapshot(`${JSON.stringify(manifest)}\n${Object.entries(manifest.metrics ?? manifest).map(([k, v]) => `${k}: ${v}`).join('\n')}`)
    : {}
  const metricRows = Object.entries(metrics).filter(([key]) => key !== 'criticalpath_derived')
  return <div className="border-t border-gray-800 bg-[#0d0d0d] px-3 py-2 font-mono" data-testid="job-details">
    <div className="mb-2 flex flex-wrap items-center gap-2">
      <span className="text-[9px] uppercase tracking-wider text-gray-500">Result</span>
      <span className={`rounded border px-1.5 py-0.5 text-[9px] uppercase tracking-wider ${
        failed ? 'border-red-500/40 bg-red-500/10 text-red-400'
          : inconclusive ? 'border-orange-500/40 bg-orange-500/10 text-orange-400'
            : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400'}`}>
        {outcome ?? value?.status ?? 'unknown'}
      </span>
      <span className="text-[10px] text-gray-400">{value?.kind}</span>
      <span className="text-[10px] text-gray-500">{value?.design}</span>
      <span className="text-[9px] text-gray-700">{runtime}</span>
      <span className="ml-auto text-[9px] text-gray-700">{state === 'idle' ? 'loading…' : state === 'unavailable' ? 'live data only' : value?.id}</span>
    </div>
    {value?.error && <div className="mb-2 rounded border border-red-900/60 bg-red-950/20 px-2 py-1 text-[10px] text-red-300">{value.error}</div>}
    {metricRows.length > 0 && (
      <div className="mb-2 grid grid-cols-2 lg:grid-cols-4 gap-1.5">
        {metricRows.map(([key, metricValue]) => (
          <div key={key} className="rounded border border-gray-800 bg-[#0b0b0b] px-2 py-1">
            <div className="truncate text-[8px] uppercase tracking-wider text-gray-600">{String(key).replace(/_/g, ' ')}</div>
            <div className="truncate text-[11px] text-gray-200">{String(metricValue)}</div>
          </div>
        ))}
      </div>
    )}
    {!value?.error && metricRows.length === 0 && !inconclusive && !failed && (
      <div className="mb-2 text-[10px] text-gray-600">No structured metrics on this result.</div>
    )}
    {evidence && <div className="mb-2 flex flex-wrap gap-2 text-[9px] uppercase tracking-wider">{Object.entries(evidence).filter(([key]) => key !== 'run_id').map(([key, href]) => <a key={key} href={`${API_URL}${href}`} target="_blank" rel="noreferrer" className="text-cyan-400 hover:text-cyan-200">{key}</a>)}</div>}
    <details className="rounded border border-gray-800 bg-[#0b0b0b]">
      <summary className="cursor-pointer list-none px-2.5 py-1.5 text-[9px] uppercase tracking-wider text-gray-600 hover:text-gray-400 [&::-webkit-details-marker]:hidden">Execution details</summary>
      <dl className="grid grid-cols-2 lg:grid-cols-3 gap-x-4 gap-y-2 border-t border-gray-800 px-2.5 py-2">
        <Field label="job">{value?.id}</Field><Field label="tool / flow">{value?.kind}</Field><Field label="design">{value?.design}</Field>
        <Field label="created">{value?.created_at}</Field><Field label="started">{value?.started_at}</Field><Field label="runtime">{runtime}</Field>
        <Field label="outcome">{value?.outcome ?? value?.status}</Field><Field label="return code">{value?.returncode}</Field><Field label="target">{manifest?.target ?? value?.params?.target}</Field>
        <Field label="stage">{manifest?.stage ?? manifest?.flow_mode}</Field><Field label="source digest">{manifest?.rtl_sha256 ?? manifest?.hardware_project_digest}</Field><Field label="constraint digest">{manifest?.constraints_sha256}</Field>
      </dl>
    </details>
  </div>
}
