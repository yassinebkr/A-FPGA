import { DEFAULT_FOOTPRINT, getDieMetrics } from './physicalLayoutModel'

export default function PhysicalLayoutCanvas({ view, transform, cellLevel, selectedComponent, pathComponents, onSelectComponent, attachContainer, onPointerDown, onPointerMove, onPointerUp }) {
  const metrics = getDieMetrics(view)
  const footprint = view.nominal_footprint ?? DEFAULT_FOOTPRINT
  const [ox, oy] = metrics.origin
  return (
    <div
      ref={attachContainer}
      className="relative min-h-0 flex-1 overflow-hidden bg-[#0a0a0a]"
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={onPointerUp}
      aria-label="Physical layout canvas: drag to pan, wheel to zoom"
    >
      <svg className="absolute inset-0 h-full w-full touch-none select-none" role="img" aria-label={`Physical layout for ${view.identity?.design ?? 'design'}`}>
        <g transform={`translate(${transform.x}, ${transform.y}) scale(${transform.scale})`}>
          <rect x={ox} y={oy} width={metrics.width} height={metrics.height} fill="#0d0d0d" stroke="#525252" strokeWidth={4 / transform.scale} />
          {view.rows?.map((row, index) => (
            <rect key={`row-${index}`} x={row.x} y={row.y}
              width={row.num_x * row.step_x} height={row.num_y * row.step_y || footprint.h}
              fill="rgba(82,82,82,0.10)" stroke="none" />
          ))}
          {view.components.map((component) => {
            const isSelected = selectedComponent?.name === component.name
            const onPath = pathComponents.some((item) => item.name === component.name)
            const width = cellLevel ? footprint.w : 4 / transform.scale
            const height = cellLevel ? footprint.h : 4 / transform.scale
            return (
              <rect
                key={component.name}
                x={component.x} y={component.y}
                width={width} height={height}
                fill={isSelected ? '#34d399' : onPath ? '#fbbf24' : '#3f3f46'}
                opacity={isSelected || onPath ? 1 : 0.85}
                stroke={isSelected ? '#a7f3d0' : onPath ? '#fde68a' : 'none'}
                strokeWidth={(isSelected || onPath ? 6 : 1) / transform.scale}
                role="button"
                tabIndex={0}
                aria-label={`placed cell ${component.name}, ${component.macro}`}
                onClick={(event) => {
                  event.stopPropagation()
                  if (component.graph_id) onSelectComponent?.(component.graph_id)
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    if (component.graph_id) onSelectComponent?.(component.graph_id)
                  }
                }}
                className="cursor-pointer focus:outline-none"
              />
            )
          })}
          {view.pins?.filter((pin) => pin.x != null).map((pin) => (
            <g key={`pin-${pin.name}`}>
              <circle cx={pin.x} cy={pin.y} r={8 / transform.scale} fill={pin.direction === 'INPUT' ? '#8b9dc3' : '#c3a78b'} />
              {cellLevel && (
                <text x={pin.x + 12 / transform.scale} y={pin.y} fontSize={10 / transform.scale} fill="#8b9dc3" fontFamily="ui-monospace, monospace">{pin.name}</text>
              )}
            </g>
          ))}
        </g>
      </svg>
      <div className="pointer-events-none absolute bottom-2 left-2 text-[8px] text-gray-700">
        {cellLevel ? 'cell level' : 'block level — zoom in for cells'}
      </div>
    </div>
  )
}
