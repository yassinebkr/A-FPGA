export default function PhysicalLayoutStatus({ jobId, error, view, loading }) {
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
  return null
}
