import { memo } from 'react'
import CellGlyph from './CellGlyph'

const SchematicContent = memo(function SchematicContent({
  graph, layout, positionById, routes, emphasis, selection, pathHighlight, selectNode,
}) {
  return (
    <>
      {layout.size && <rect x={-200} y={-200} width={layout.size.w + 400} height={layout.size.h + 400} fill="url(#schematic-dot-grid)" />}
      {routes.map((route, index) => {
        const emphasized = emphasis ? emphasis.routes.has(route.net) : true
        return <polyline key={`${route.net}-${index}`} points={route.points.map((point) => point.join(',')).join(' ')} fill="none" stroke={selection?.type === 'net' && selection.id === route.net ? '#fbbf24' : '#3f3f46'} strokeWidth={emphasis ? (emphasized ? 1.4 : 0.6) : 1} opacity={emphasis ? (emphasized ? 1 : 0.15) : 0.8} strokeLinejoin="round" />
      })}
      {(graph.nodes ?? []).map((node) => {
        const position = positionById.get(node.id)
        if (!position) return null
        return <CellGlyph key={node.id} node={node} pos={position} selected={selection?.type === 'node' && selection.id === node.id} dimmed={emphasis ? !emphasis.nodes.has(node.id) : false} pathHighlighted={Boolean(pathHighlight?.nodes.has(node.id))} onSelect={selectNode} />
      })}
    </>
  )
})

const MinimapNodes = memo(function MinimapNodes({ positions, scale }) {
  return positions.map((position) => <rect key={position.id} x={position.x * scale} y={position.y * scale} width={Math.max(position.w * scale, 1.5)} height={Math.max(position.h * scale, 1)} fill="#525252" />)
})

export default function GateSchematicCanvas({
  containerRef, svgRef, graph, layout, positionById, loading, scope, view, reduceMotion,
  routes, emphasis, selection, pathHighlight, selectNode, onKeyDown,
  onPointerDown, onPointerMove, onPointerUp, minimap, viewportRect,
  onMinimapClick,
}) {
  return (
    <div ref={containerRef} className="relative min-w-0 flex-1 overflow-hidden bg-[#0a0a0a]" tabIndex={0} onKeyDown={onKeyDown} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerLeave={onPointerUp} aria-label="Gate schematic canvas: arrow keys move the selection, plus and minus zoom, f fits">
      {graph && layout && (graph.nodes?.length ?? 0) > 0 && <svg ref={svgRef} className="absolute inset-0 h-full w-full touch-none select-none" role="img" aria-label={`Gate schematic for module ${graph.module}`}>
        <defs><pattern id="schematic-dot-grid" width={24} height={24} patternUnits="userSpaceOnUse"><circle cx={1} cy={1} r={0.8} fill="#1f1f27" /></pattern></defs>
        <g transform={`translate(${view.x}, ${view.y}) scale(${view.scale})`} style={reduceMotion ? undefined : { transition: 'opacity 0.15s' }}>
          <SchematicContent graph={graph} layout={layout} positionById={positionById} routes={routes} emphasis={emphasis} selection={selection} pathHighlight={pathHighlight} selectNode={selectNode} />
        </g>
      </svg>}
      {graph && (graph.nodes?.length ?? 0) === 0 && !loading && <div className="absolute inset-0 flex items-center justify-center text-[10px] text-gray-600" data-testid="gate-schematic-empty">This scope renders no nodes — the selected module or cone has no visible logic within the node limit.</div>}
      {minimap && layout && (graph?.nodes?.length ?? 0) > 0 && <div className="absolute bottom-2 right-2 rounded border border-gray-800 bg-[#0d0d0d] p-1" data-testid="gate-schematic-minimap"><svg width={minimap.box.w} height={minimap.box.h} className="cursor-pointer" onClick={onMinimapClick}><MinimapNodes positions={layout.positions} scale={minimap.scale} />{viewportRect && <rect x={viewportRect.x} y={viewportRect.y} width={viewportRect.w} height={viewportRect.h} fill="rgba(52,211,153,0.08)" stroke="#34d399" strokeWidth={0.8} />}</svg></div>}
      <div className="pointer-events-none absolute bottom-2 left-2 text-[8px] text-gray-700">{graph ? `${graph.counts?.nodes} nodes · ${graph.counts?.cells} cells · ${graph.counts?.nets} nets · limit ${scope?.limit}` : ''}</div>
    </div>
  )
}
