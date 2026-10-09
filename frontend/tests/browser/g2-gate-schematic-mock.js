// Shared gate-netlist endpoint mocks for the G2 browser suite: the same
// bounded-graph contract the backend serves, fixture-scaled.

export const ARTIFACT_SHA = 'a'.repeat(64)
export const NETLIST_SHA = 'b'.repeat(64)

// Small counter fixture mirroring the backend gate-graph/v1 contract: the
// same shape /tmp-g1-e2e serves from a real synthesis, scaled down.
export function counterGraph({ module = 'counter8', truncated = false } = {}) {
  const nodes = module === 'alu_slice' ? [
    { id: 'port:A', kind: 'port', name: 'A', direction: 'input', width: 1, nets: ['n0'], module: 'alu_slice' },
    { id: 'cell:_xor_', kind: 'cell', name: '_xor_', type: 'sky130_fd_sc_hd__xor2_1', module: 'alu_slice',
      hierarchy: null, directions_known: true, black_box: false, source: 'rtl/alu_slice.v:7',
      pins: { A: ['n0'], X: ['n1'] }, pin_directions: { A: 'input', X: 'output' } },
  ] : [
    { id: 'port:clk', kind: 'port', name: 'clk', direction: 'input', width: 1, nets: ['n0'], module: 'counter8' },
    { id: 'port:q', kind: 'port', name: 'q', direction: 'output', width: 1, nets: ['n2'], module: 'counter8' },
    { id: 'cell:_ff0_', kind: 'cell', name: '_ff0_', type: 'sky130_fd_sc_hd__dfxtp_1', module: 'counter8',
      hierarchy: null, directions_known: true, black_box: false, source: 'rtl/counter8.v:12',
      pins: { CLK: ['n0'], D: ['n1'], Q: ['n2'] }, pin_directions: { CLK: 'input', D: 'input', Q: 'output' } },
    { id: 'cell:_slice_', kind: 'cell', name: '_slice_', type: 'alu_slice', module: 'counter8',
      hierarchy: 'alu_slice', directions_known: true, black_box: false, source: 'rtl/counter8.v:15',
      pins: { A: ['n2'], S: ['n1'] }, pin_directions: { A: 'input', S: 'output' } },
    { id: 'cell:_and0_', kind: 'cell', name: '_and0_', type: 'sky130_fd_sc_hd__and2_0', module: 'counter8',
      hierarchy: null, directions_known: true, black_box: false, source: null,
      pins: { A: ['n0'], X: ['n3'] }, pin_directions: { A: 'input', X: 'output' } },
  ]
  const nets = module === 'alu_slice' ? [
    { id: 'net:alu_slice:n0', module: 'alu_slice', name: 'A', names: ['A'], source: null,
      drivers: [{ node: 'port:A', pin: null }], loads: [{ node: 'cell:_xor_', pin: 'A' }] },
  ] : [
    { id: 'net:counter8:n0', module: 'counter8', name: 'clk', names: ['clk'], source: 'rtl/counter8.v:2',
      drivers: [{ node: 'port:clk', pin: null }], loads: [{ node: 'cell:_ff0_', pin: 'CLK' }, { node: 'cell:_and0_', pin: 'A' }] },
    { id: 'net:counter8:n2', module: 'counter8', name: 'q', names: ['q'], source: null,
      drivers: [{ node: 'cell:_ff0_', pin: 'Q' }], loads: [{ node: 'port:q', pin: null }, { node: 'cell:_slice_', pin: 'A' }] },
  ]
  const edges = [
    { net: 'net:counter8:n0', from: { node: 'port:clk', pin: null }, to: { node: 'cell:_ff0_', pin: 'CLK' } },
    { net: 'net:counter8:n2', from: { node: 'cell:_ff0_', pin: 'Q' }, to: { node: 'port:q', pin: null } },
  ]
  return {
    schema: 'gate-graph/v1', job_id: 'j-a-syn', artifact_sha256: ARTIFACT_SHA,
    netlist_sha256: NETLIST_SHA, top_module: 'counter8', module, depth: 1,
    node_limit: 500, cone: null, modules: ['alu_slice', 'counter8'],
    nodes, nets, edges, truncated, truncated_reasons: truncated ? ['node limit reached while adding cells'] : [],
    counts: { nodes: nodes.length, nets: nets.length, cells: nodes.filter((n) => n.kind === 'cell').length, hierarchical: 1 },
    completeness: { complete: true, truncated: false, black_box_modules: [], unsupported_primitives: [], module_count: 2, cell_count: 5, net_count: 8 },
  }
}

export function installNetlistMock(page, { jobId = 'j-a-syn', graphHandler } = {}) {
  page.route(`http://127.0.0.1:8000/api/jobs/${jobId}/netlist`, (route) =>
    route.fulfill({ json: {
      schema: 'gate-netlist/v1', available: true, artifact_sha256: ARTIFACT_SHA,
      repository_sha256: 'c'.repeat(64), job_id: jobId, project: 'proj-a',
      source_sha256: 'd'.repeat(64), constraints_sha256: null, netlist_sha256: NETLIST_SHA,
      tool: 'yosys', tool_version: 'Yosys 0.50', library: 'sky130_fd_sc_hd',
      top_module: 'counter8', module_count: 2, cell_count: 5, net_count: 8,
      completeness: { complete: true, black_box_modules: [], unsupported_primitives: [] },
      modules: ['alu_slice', 'counter8'],
    } }))
  page.route(new RegExp(`http://127\\.0\\.0\\.1:8000/api/jobs/${jobId}/netlist/graph`), (route) => {
    const url = new URL(route.request().url())
    const module = url.searchParams.get('module') ?? 'counter8'
    const limit = Number(url.searchParams.get('limit') ?? 500)
    const payload = graphHandler ? graphHandler(url) : counterGraph({ module, truncated: limit <= 2 })
    route.fulfill({ json: payload })
  })
}


