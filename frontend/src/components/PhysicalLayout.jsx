import PhysicalLayoutCanvas from './PhysicalLayoutCanvas'
import PhysicalLayoutStatus from './PhysicalLayoutStatus'
import PhysicalLayoutToolbar from './PhysicalLayoutToolbar'
import { findPathComponents, findSelectedComponent } from './physicalLayoutModel'
import { usePhysicalLayoutData, usePhysicalLayoutViewport } from '../hooks/usePhysicalLayout'

export default function PhysicalLayout({ job, selection, pathCells, onSelectComponent, onAvailability }) {
  const jobId = job?.id ?? null
  const { view, error, loading } = usePhysicalLayoutData(jobId, onAvailability)
  const selectedComponent = findSelectedComponent(view, selection)
  const pathComponents = findPathComponents(view, pathCells)
  const viewport = usePhysicalLayoutViewport({ view, selectedComponent })

  if (!jobId || error || (view && view.available === false) || !view || loading) {
    return <PhysicalLayoutStatus jobId={jobId} error={error} view={view} loading={loading} />
  }

  return (
    <div className="flex h-full min-h-0 flex-col font-mono" data-testid="physical-layout">
      <PhysicalLayoutToolbar
        view={view}
        onZoomIn={viewport.zoomIn}
        onZoomOut={viewport.zoomOut}
        onFit={viewport.fit}
        onReset={viewport.reset}
      />
      <PhysicalLayoutCanvas
        view={view}
        transform={viewport.transform}
        cellLevel={viewport.cellLevel}
        selectedComponent={selectedComponent}
        pathComponents={pathComponents}
        onSelectComponent={onSelectComponent}
        attachContainer={viewport.attachContainer}
        onPointerDown={viewport.onPointerDown}
        onPointerMove={viewport.onPointerMove}
        onPointerUp={viewport.onPointerUp}
      />
    </div>
  )
}
