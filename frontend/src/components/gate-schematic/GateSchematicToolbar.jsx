import { ChevronLeft, Maximize2, RotateCcw, Search, ZoomIn, ZoomOut } from 'lucide-react'

export default function GateSchematicToolbar({
  meta, breadcrumb, graph, scope, search, setSearch, searchResults,
  openScope, goBack, clearCone, selectNode, selectNet, setView, view, fit,
  minScale, maxScale,
}) {
  return (
    <>
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-gray-800 bg-[#0f0f0f] px-3 py-1.5 text-[9px] text-gray-500">
        <span className="uppercase tracking-wider text-gray-400">gate netlist</span>
        {meta?.top_module && <span>top <b className="font-normal text-gray-300">{meta.top_module}</b></span>}
        {meta?.library && <span>lib <b className="font-normal text-gray-300">{meta.library}</b></span>}
        {meta?.tool && <span>{meta.tool}{meta.tool_version ? ` ${meta.tool_version}` : ''}</span>}
        {meta?.artifact_sha256 && <span title={meta.artifact_sha256}>artifact {meta.artifact_sha256.slice(0, 10)}</span>}
        {meta?.netlist_sha256 && <span title={meta.netlist_sha256}>netlist {meta.netlist_sha256.slice(0, 10)}</span>}
        {meta?.cell_count != null && <span>{meta.cell_count} cells</span>}
        {meta?.completeness?.black_box_modules?.length > 0 && <span className="text-orange-400" title={meta.completeness.black_box_modules.join(', ')}>{meta.completeness.black_box_modules.length} black-box module(s)</span>}
        {meta?.completeness?.unsupported_primitives?.length > 0 && <span className="text-amber-400" title={meta.completeness.unsupported_primitives.join(', ')}>{meta.completeness.unsupported_primitives.length} primitive(s) with unknown pin directions</span>}
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-gray-800 bg-[#0d0d0d] px-3 py-1.5">
        <div className="flex items-center gap-1">
          {breadcrumb.length > 1 && <button type="button" onClick={goBack} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Back to parent scope" aria-label="Back to parent scope"><ChevronLeft className="h-3 w-3" /></button>}
          <span className="text-[10px] text-gray-300" data-testid="gate-schematic-scope">
            {breadcrumb.map((item, index) => <span key={`${item.module}-${index}`}>{index > 0 && <span className="text-gray-700"> / </span>}<span className={index === breadcrumb.length - 1 ? 'text-gray-100' : 'text-gray-500'}>{item.label}</span></span>)}
          </span>
        </div>
        {(graph?.modules?.length ?? 0) > 1 && <select value={scope?.module ?? ''} onChange={(event) => openScope(event.target.value)} className="rounded border border-gray-800 bg-[#090909] px-1.5 py-0.5 text-[9px] text-gray-400 outline-none" aria-label="Hierarchy scope">{(graph?.modules ?? []).map((module) => <option key={module} value={module}>{module}</option>)}</select>}
        {scope?.cone && <span className="flex items-center gap-1 rounded border border-amber-500/40 px-1.5 py-0.5 text-[9px] text-amber-300" data-testid="gate-schematic-cone">cone: {scope.cone}<button type="button" onClick={clearCone} className="text-amber-400 hover:text-amber-200" title="Clear cone restriction">clear</button></span>}
        <div className="relative ml-auto">
          <label className="flex items-center gap-1 rounded border border-gray-800 bg-[#090909] px-2"><Search className="h-3 w-3 text-gray-600" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search instances, cells, signals, sources" aria-label="Search instances, cells, signals, sources" className="w-52 bg-transparent py-1 text-[10px] text-gray-300 outline-none placeholder:text-gray-700" /></label>
          {searchResults.length > 0 && <div className="absolute right-0 top-7 z-20 w-64 rounded border border-gray-700 bg-[#151515] p-1 shadow-xl" data-testid="gate-schematic-search-results">{searchResults.map((result) => <button key={`${result.type}:${result.id}`} type="button" className="block w-full truncate rounded px-2 py-1 text-left text-[10px] text-gray-300 hover:bg-white/5" onClick={() => { setSearch(''); if (result.type === 'node') selectNode(result.id); else selectNet(result.id) }}>{result.label} <span className="text-gray-600">{result.sub}</span></button>)}</div>}
        </div>
        <div className="flex items-center gap-1">
          <button type="button" onClick={() => setView((v) => ({ ...v, scale: Math.min(v.scale * 1.25, maxScale) }))} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Zoom in" aria-label="Zoom in"><ZoomIn className="h-3 w-3" /></button>
          <button type="button" onClick={() => setView((v) => ({ ...v, scale: Math.max(v.scale / 1.25, minScale) }))} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Zoom out" aria-label="Zoom out"><ZoomOut className="h-3 w-3" /></button>
          <button type="button" onClick={fit} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Fit to view" aria-label="Fit to view"><Maximize2 className="h-3 w-3" /></button>
          <button type="button" onClick={() => { setView({ x: 0, y: 0, scale: 1 }); fit() }} className="rounded border border-gray-800 p-1 text-gray-500 hover:border-gray-600 hover:text-gray-300" title="Reset view" aria-label="Reset view"><RotateCcw className="h-3 w-3" /></button>
          <span className="ml-1 text-[9px] text-gray-600">{Math.round(view.scale * 100)}%</span>
        </div>
      </div>
    </>
  )
}
