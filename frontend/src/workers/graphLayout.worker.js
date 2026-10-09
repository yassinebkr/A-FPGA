// Deterministic layered layout for bounded gate graphs (Phase G2).
//
// Runs off the main thread so layout work can never block chat, job
// streaming, or cancellation. The algorithm is intentionally simple and
// stable: input ports anchor layer 0, every cell sits one layer below its
// deepest driver, output ports sit right of their deepest driver, and nodes
// inside a layer are ordered by name. Cycles (combinational loops in real
// netlists) freeze after a bounded number of passes instead of oscillating.
//
// Message contract:
//   in:  { id, graph: { nodes, edges } }
//   out: { id, layout: { positions: [{id,x,y,w,h}], routes: [{net, points}], size: {w,h} } }

const CELL_W = 120
const CELL_H = 34
const PORT_W = 64
const PORT_H = 20
const LAYER_GAP = 80
const ROW_GAP = 14
const PAD = 24

function nodeSize(node) {
  return node.kind === 'port'
    ? { w: PORT_W, h: PORT_H }
    : { w: CELL_W, h: CELL_H }
}

function layeredLayout(graph) {
  const nodes = graph.nodes ?? []
  const edges = graph.edges ?? []
  if (!nodes.length) return { positions: [], routes: [], size: { w: PAD * 2, h: PAD * 2 } }

  const byId = new Map(nodes.map((node) => [node.id, node]))
  const incoming = new Map()
  const outgoing = new Map()
  for (const edge of edges) {
    const from = edge.from?.node
    const to = edge.to?.node
    if (!from || !to || !byId.has(from) || !byId.has(to)) continue
    if (!incoming.has(to)) incoming.set(to, [])
    incoming.get(to).push(from)
    if (!outgoing.has(from)) outgoing.set(from, [])
    outgoing.get(from).push(to)
  }

  // Layer assignment. Input ports start at layer 0; cells take
  // max(layer(driver)) + 1; output ports go right of their drivers.
  const layer = new Map()
  const isInputPort = (node) => node.kind === 'port' && (node.direction === 'input' || node.direction === 'inout')
  const isOutputPort = (node) => node.kind === 'port' && node.direction === 'output'
  for (const node of nodes) {
    if (isInputPort(node)) layer.set(node.id, 0)
  }
  const cellIds = nodes.filter((node) => node.kind === 'cell').map((node) => node.id)
  const passes = Math.min(nodes.length + 1, 256)
  for (let pass = 0; pass < passes; pass += 1) {
    let changed = false
    for (const id of cellIds) {
      const drivers = incoming.get(id) ?? []
      let value = 1
      for (const driver of drivers) {
        const driverLayer = layer.get(driver)
        if (driverLayer !== undefined) value = Math.max(value, driverLayer + 1)
      }
      if (layer.get(id) !== value) {
        layer.set(id, value)
        changed = true
      }
    }
    if (!changed) break
  }
  // Feed-forward cells that no driver reached stay at layer 1.
  for (const id of cellIds) if (!layer.has(id)) layer.set(id, 1)
  let maxLayer = Math.max(0, ...cellIds.map((id) => layer.get(id) ?? 1))
  for (const node of nodes) {
    if (!isOutputPort(node)) continue
    const drivers = incoming.get(node.id) ?? []
    let value = 1
    for (const driver of drivers) {
      const driverLayer = layer.get(driver)
      if (driverLayer !== undefined) value = Math.max(value, driverLayer + 1)
    }
    layer.set(node.id, value)
    maxLayer = Math.max(maxLayer, value)
  }
  for (const node of nodes) if (!layer.has(node.id)) layer.set(node.id, 0)

  // Deterministic placement: sort by layer, then by name/id.
  const ordered = [...nodes].sort((a, b) => (layer.get(a.id) - layer.get(b.id))
    || String(a.name ?? a.id).localeCompare(String(b.name ?? b.id))
    || String(a.id).localeCompare(String(b.id)))
  const rows = new Map()
  const positions = []
  let maxRight = 0
  let maxBottom = 0
  for (const node of ordered) {
    const level = layer.get(node.id) ?? 0
    const row = rows.get(level) ?? 0
    rows.set(level, row + 1)
    const { w, h } = nodeSize(node)
    const x = PAD + level * (CELL_W + LAYER_GAP)
    const y = PAD + row * (CELL_H + ROW_GAP)
    positions.push({ id: node.id, x, y, w, h })
    maxRight = Math.max(maxRight, x + w)
    maxBottom = Math.max(maxBottom, y + h)
  }
  const posById = new Map(positions.map((item) => [item.id, item]))

  // Orthogonal routes: leave the driver's right edge, elbow at the midpoint,
  // enter the load's left edge.
  const routes = []
  for (const edge of edges) {
    const from = posById.get(edge.from?.node)
    const to = posById.get(edge.to?.node)
    if (!from || !to) continue
    const x1 = from.x + from.w
    const y1 = from.y + from.h / 2
    const x2 = to.x
    const y2 = to.y + to.h / 2
    const midX = (x1 + x2) / 2
    const points = x2 >= x1
      ? [[x1, y1], [midX, y1], [midX, y2], [x2, y2]]
      : [[x1, y1], [x1 + (LAYER_GAP / 2), y1], [x1 + (LAYER_GAP / 2), y2], [x2, y2]]
    routes.push({ net: edge.net, from: edge.from.node, to: edge.to.node, points })
  }
  return { positions, routes, size: { w: maxRight + PAD, h: maxBottom + PAD } }
}

self.onmessage = (event) => {
  const { id, graph } = event.data ?? {}
  try {
    self.postMessage({ id, layout: layeredLayout(graph) })
  } catch (error) {
    self.postMessage({ id, error: String(error) })
  }
}
