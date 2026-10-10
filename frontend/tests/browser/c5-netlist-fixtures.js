// Deterministic C5 qualification fixtures.
//
// The large case represents a 150k-cell artifact while returning only the
// server-enforced bounded graph window. This keeps browser tests repeatable
// and exercises the same truncation contract the production API exposes.

export const C5_ARTIFACT_SHA = '5'.repeat(64)
export const C5_NETLIST_SHA = '6'.repeat(64)

export const C5_BENCHMARKS = {
  small: { name: 'small', module: 'small_top', cells: 16 },
  medium: { name: 'medium', module: 'medium_top', cells: 160 },
  large: { name: 'large', module: 'large_top', cells: 150000 },
}

function cellNode(index, module) {
  const id = `cell:u${String(index).padStart(5, '0')}`
  const input = `n${String(index).padStart(5, '0')}`
  const output = `n${String(index + 1).padStart(5, '0')}`
  const sequential = index % 17 === 0
  return {
    id,
    kind: 'cell',
    name: `u${String(index).padStart(5, '0')}`,
    type: sequential ? 'sky130_fd_sc_hd__dfxtp_1' : 'sky130_fd_sc_hd__and2_1',
    module,
    hierarchy: index % 40 === 0 ? `block_${Math.floor(index / 40)}` : null,
    directions_known: true,
    black_box: false,
    source: `rtl/${module}.v:${index + 2}`,
    pins: { A: [input], X: [output] },
    pin_directions: { A: 'input', X: 'output' },
  }
}

function edge(from, to, net) {
  return { net, from: { node: from, pin: from.startsWith('port:') ? null : 'X' }, to: { node: to, pin: to.startsWith('port:') ? null : 'A' } }
}

export function c5Graph({ benchmark = 'small', limit = 500 } = {}) {
  const spec = C5_BENCHMARKS[benchmark] ?? C5_BENCHMARKS.small
  const visibleCells = Math.min(spec.cells, Math.max(0, limit - 2))
  const nodes = [
    { id: 'port:in', kind: 'port', name: 'in', direction: 'input', width: 1, nets: ['n00000'], module: spec.module },
    ...Array.from({ length: visibleCells }, (_, index) => cellNode(index, spec.module)),
    { id: 'port:out', kind: 'port', name: 'out', direction: 'output', width: 1, nets: [`n${String(visibleCells).padStart(5, '0')}`], module: spec.module },
  ]
  const nets = Array.from({ length: visibleCells + 1 }, (_, index) => {
    const net = `net:${spec.module}:n${String(index).padStart(5, '0')}`
    const driver = index === 0 ? 'port:in' : `cell:u${String(index - 1).padStart(5, '0')}`
    const load = index === visibleCells ? 'port:out' : `cell:u${String(index).padStart(5, '0')}`
    return {
      id: net,
      module: spec.module,
      name: `n${String(index).padStart(5, '0')}`,
      names: [`n${String(index).padStart(5, '0')}`],
      source: `rtl/${spec.module}.v:${index + 2}`,
      drivers: [{ node: driver, pin: driver.startsWith('port:') ? null : 'X' }],
      loads: [{ node: load, pin: load.startsWith('port:') ? null : 'A' }],
    }
  })
  const edges = Array.from({ length: visibleCells + 1 }, (_, index) => edge(
    index === 0 ? 'port:in' : `cell:u${String(index - 1).padStart(5, '0')}`,
    index === visibleCells ? 'port:out' : `cell:u${String(index).padStart(5, '0')}`,
    `net:${spec.module}:n${String(index).padStart(5, '0')}`,
  ))
  const truncated = visibleCells < spec.cells
  return {
    schema: 'gate-graph/v1',
    job_id: `j-c5-${spec.name}`,
    artifact_sha256: C5_ARTIFACT_SHA,
    netlist_sha256: C5_NETLIST_SHA,
    top_module: spec.module,
    module: spec.module,
    depth: 1,
    node_limit: limit,
    cone: null,
    modules: [spec.module],
    nodes,
    nets,
    edges,
    truncated,
    truncated_reasons: truncated ? ['node limit reached while adding cells'] : [],
    counts: { nodes: nodes.length, nets: nets.length, cells: visibleCells, hierarchical: 0 },
    completeness: {
      complete: !truncated,
      truncated,
      black_box_modules: [],
      unsupported_primitives: [],
      module_count: 1,
      cell_count: spec.cells,
      net_count: spec.cells + 1,
    },
  }
}

export function c5Metadata(benchmark = 'small', jobId = `j-c5-${benchmark}`) {
  const spec = C5_BENCHMARKS[benchmark] ?? C5_BENCHMARKS.small
  return {
    schema: 'gate-netlist/v1',
    available: true,
    artifact_sha256: C5_ARTIFACT_SHA,
    repository_sha256: '7'.repeat(64),
    job_id: jobId,
    project: 'proj-a',
    source_sha256: '8'.repeat(64),
    constraints_sha256: null,
    netlist_sha256: C5_NETLIST_SHA,
    tool: 'yosys',
    tool_version: 'Yosys 0.50',
    library: 'sky130_fd_sc_hd',
    top_module: spec.module,
    module_count: 1,
    cell_count: spec.cells,
    net_count: spec.cells + 1,
    completeness: { complete: true, black_box_modules: [], unsupported_primitives: [] },
    modules: [spec.module],
  }
}

