export const MIN_SCALE = 0.02
export const MAX_SCALE = 40
export const DEFAULT_FOOTPRINT = { w: 460, h: 2720 }

export function findSelectedComponent(view, selection) {
  const selectedId = selection?.type === 'node' ? selection.id : null
  return view?.components?.find((component) => component.graph_id === selectedId) ?? null
}

export function findPathComponents(view, pathCells) {
  const pathComponentNames = new Set((pathCells ?? []).map((name) => `cell:${name}`))
  return view?.components?.filter((component) => pathComponentNames.has(component.graph_id)) ?? []
}

export function getDieMetrics(view) {
  const die = view?.die
  if (!die) return null
  return {
    origin: [die.llx, die.lly],
    width: die.urx - die.llx,
    height: die.ury - die.lly,
  }
}

export function fitTransform(containerSize, die) {
  if (!containerSize?.w || !containerSize?.h || !die) return null
  const width = die.urx - die.llx
  const height = die.ury - die.lly
  const scale = Math.min(
    Math.max(Math.min(containerSize.w / width, containerSize.h / height) * 0.9, MIN_SCALE),
    MAX_SCALE)
  return {
    scale,
    x: (containerSize.w - width * scale) / 2,
    y: (containerSize.h - height * scale) / 2,
  }
}

export function zoomTransform(current, factor, point) {
  const scale = Math.min(Math.max(current.scale * factor, MIN_SCALE), MAX_SCALE)
  const ratio = scale / current.scale
  return {
    scale,
    x: point.x - (point.x - current.x) * ratio,
    y: point.y - (point.y - current.y) * ratio,
  }
}
