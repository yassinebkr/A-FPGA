import { pinSide } from './utils'

const SEQ_CELL = /df|latch|sram|reg/i

export default function CellGlyph({ node, pos, selected, dimmed, pathHighlighted, onSelect }) {
  const isSequential = SEQ_CELL.test(node.type ?? '')
  const pins = Object.keys(node.pins ?? {})
  const leftPins = pins.filter((pin) => pinSide(node, pin) === 'left')
  const rightPins = pins.filter((pin) => pinSide(node, pin) === 'right')
  const stroke = node.black_box ? '#525252' : '#3f3f46'
  const dash = node.black_box ? '3 2' : undefined
  return (
    <g
      transform={`translate(${pos.x}, ${pos.y})`}
      opacity={dimmed ? 0.25 : 1}
      onClick={(event) => { event.stopPropagation(); onSelect(node) }}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect(node)
        }
      }}
      tabIndex={0}
      role="button"
      aria-label={`${node.kind === 'cell' ? 'cell' : 'port'} ${node.name}${node.type ? `, type ${node.type}` : ''}${node.hierarchy ? `, hierarchy ${node.hierarchy}` : ''}`}
      className="cursor-pointer focus:outline-none"
    >
      {node.kind === 'port' ? (
        <>
          <rect width={pos.w} height={pos.h} fill="#0d0d0d" stroke={stroke} strokeWidth={1} rx={2} strokeDasharray={dash} />
          <text x={6} y={13} fill="#8b9dc3" fontSize={9} fontFamily="ui-monospace, monospace">{node.name}</text>
          {node.direction === 'input' && <path d={`M ${pos.w - 8} 6 L ${pos.w - 2} ${pos.h / 2} L ${pos.w - 8} ${pos.h - 6}`} fill="#4b5563" />}
          {node.direction === 'output' && <path d={`M 8 6 L 2 ${pos.h / 2} L 8 ${pos.h - 6}`} fill="#4b5563" />}
        </>
      ) : (
        <>
          {selected && <rect x={-4} y={-4} width={pos.w + 8} height={pos.h + 8} fill="none" stroke="#34d399" strokeWidth={1.2} rx={3} />}
          {pathHighlighted && !selected && <rect x={-3.5} y={-3.5} width={pos.w + 7} height={pos.h + 7} fill="none" stroke="#fbbf24" strokeWidth={1} rx={3} />}
          <rect width={pos.w} height={pos.h} fill="#161616" stroke={selected ? '#34d399' : stroke} strokeWidth={1} rx={2} strokeDasharray={dash} />
          {node.hierarchy && <rect x={3} y={3} width={pos.w - 6} height={pos.h - 6} fill="none" stroke={stroke} strokeWidth={0.75} rx={1} />}
          {isSequential && <path d={`M 0 ${pos.h / 2 - 4} L 7 ${pos.h / 2} L 0 ${pos.h / 2 + 4} Z`} fill="#0d0d0d" stroke={stroke} strokeWidth={0.9} />}
          <text x={16} y={12} fill="#d4d4d4" fontSize={9} fontFamily="ui-monospace, monospace">{node.name.slice(0, 12)}</text>
          <text x={16} y={23} fill="#6b7280" fontSize={7.5} fontFamily="ui-monospace, monospace">{(node.type ?? '').replace(/^sky130_fd_sc_hd__/, '').slice(0, 16)}</text>
          {leftPins.map((pin, index) => (
            <g key={`l-${pin}`}>
              <line x1={-5} y1={7 + index * 5} x2={0} y2={7 + index * 5} stroke="#525252" strokeWidth={1} />
              <rect x={-6} y={6 + index * 5} width={2} height={2} fill="#737373" />
              {leftPins.length <= 4 && <text x={2} y={6 + index * 5} fill="#525252" fontSize={5} fontFamily="ui-monospace, monospace">{pin.slice(0, 4)}</text>}
            </g>
          ))}
          {rightPins.map((pin, index) => (
            <g key={`r-${pin}`}>
              <line x1={pos.w} y1={7 + index * 5} x2={pos.w + 5} y2={7 + index * 5} stroke="#525252" strokeWidth={1} />
              <rect x={pos.w + 4} y={6 + index * 5} width={2} height={2} fill="#737373" />
              {rightPins.length <= 4 && <text x={pos.w - 2} y={6 + index * 5} textAnchor="end" fill="#525252" fontSize={5} fontFamily="ui-monospace, monospace">{pin.slice(0, 4)}</text>}
            </g>
          ))}
        </>
      )}
      {node.kind === 'port' && selected && <rect x={-3} y={-3} width={pos.w + 6} height={pos.h + 6} fill="none" stroke="#34d399" strokeWidth={1.2} rx={3} />}
    </g>
  )
}
