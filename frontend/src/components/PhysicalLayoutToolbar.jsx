import { Maximize2, RotateCcw, ZoomIn, ZoomOut } from 'lucide-react'
import { getDieMetrics } from './physicalLayoutModel'

export default function PhysicalLayoutToolbar({ view, onZoomIn, onZoomOut, onFit, onReset }) {
  const metrics = getDieMetrics(view)
  const units = view.units || 1000
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-gray-800 bg-[#0f0f0f] px-3 py-1.5 text-[9px] text-gray-500">
      <span className="uppercase tracking-wider text-gray-400">physical layout</span>
      <span>design <b className="font-normal text-gray-300">{view.identity?.design ?? '—'}</b></span>
      <span title={view.identity?.def_sha256}>def {String(view.identity?.def_sha256 ?? '').slice(0, 10)}</span>
      {view.identity?.netlist_sha256 && <span title={view.identity.netlist_sha256}>netlist {view.identity.netlist_sha256.slice(0, 10)}</span>}
      {metrics && <span>die {(metrics.width / units).toFixed(1)} x {(metrics.height / units).toFixed(1)} um</span>}
      <span>{view.completeness?.component_count} placed cells</span>
      {view.completeness?.nominal_cell_footprint && (
        <span className="text-gray-600" title="DEF placement carries coordinates, not LEF dimensions; rectangles are nominal site footprints.">nominal footprints</span>
      )}
      {view.completeness?.mapping_partial && (
        <span className="text-amber-400" title="Some placed cells do not exist in the bound netlist.">partial netlist mapping</span>
      )}
      {view.completeness?.truncated && <span className="text-amber-400">view truncated</span>}
      <div className="ml-auto flex items-center gap-1">
        <button type="button" onClick={onZoomIn} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Zoom in" aria-label="Zoom in"><ZoomIn className="h-3 w-3" /></button>
        <button type="button" onClick={onZoomOut} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Zoom out" aria-label="Zoom out"><ZoomOut className="h-3 w-3" /></button>
        <button type="button" onClick={onFit} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Fit to view" aria-label="Fit to view"><Maximize2 className="h-3 w-3" /></button>
        <button type="button" onClick={onReset} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Reset view" aria-label="Reset view"><RotateCcw className="h-3 w-3" /></button>
      </div>
    </div>
  )
}
