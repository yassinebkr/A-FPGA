export default function GateSchematicTiming({ timingPaths, selectedPathIndex, selectPath, clearPath, pathMismatch }) {
  return (
    <>
      {timingPaths && timingPaths.available === false && <div className="shrink-0 border-b border-gray-800 bg-[#0d0d0d] px-3 py-1 text-[9px] text-gray-600" data-testid="gate-schematic-timing-unavailable">timing paths unavailable — {timingPaths.reason}</div>}
      {timingPaths?.paths?.length > 0 && <div className="flex shrink-0 items-center gap-1.5 overflow-x-auto border-b border-gray-800 bg-[#0d0d0d] px-3 py-1 [scrollbar-width:thin]" data-testid="gate-schematic-timing">
        <span className="shrink-0 text-[8px] uppercase tracking-wider text-gray-600">timing paths</span>
        {timingPaths.paths.map((path, index) => <button key={index} type="button" data-testid={`gate-schematic-path-${index}`} onClick={() => (selectedPathIndex === index ? clearPath() : selectPath(index))} className={`shrink-0 rounded border px-1.5 py-0.5 text-[9px] transition-colors ${selectedPathIndex === index ? 'border-amber-500/60 bg-amber-500/10 text-amber-200' : 'border-gray-800 text-gray-500 hover:border-gray-600 hover:text-gray-300'}`} title={`${path.analysis} path, corner ${path.corner ?? '?'}, clock ${path.clock ?? '?'}, slack ${path.slack_ns ?? '?'} ns`}>
          <span className={path.analysis === 'setup' ? 'text-cyan-300' : 'text-violet-300'}>{path.analysis}</span><span className="mx-1 text-gray-600">{path.corner ?? '—'}</span><span className="text-gray-500">{path.clock ?? '—'}</span><span className={`ml-1 ${Number(path.slack_ns) < 0 ? 'text-red-400' : 'text-emerald-400'}`}>{path.slack_ns} ns</span>{path.mapping?.partial && <span className="ml-1 text-amber-400" title={`unresolved: ${path.mapping.unresolved.join(', ')}`}>partial</span>}
        </button>)}
        {selectedPathIndex != null && <button type="button" onClick={clearPath} className="shrink-0 rounded border border-gray-700 px-1.5 py-0.5 text-[9px] text-gray-400 hover:bg-white/5" data-testid="gate-schematic-clear-path">clear</button>}
      </div>}
      {pathMismatch && <div className="shrink-0 border-b border-red-500/30 bg-[#140d0d] px-3 py-1 text-[9px] text-red-400" data-testid="gate-schematic-path-mismatch">{pathMismatch}</div>}
    </>
  )
}
