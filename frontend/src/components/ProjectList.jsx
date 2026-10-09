import { useState } from 'react'
import { Pencil } from 'lucide-react'
import { Button } from '@/components/ui/button'
import ModelHealthDot from '@/components/ModelHealthDot'
import { healthSuffix } from '@/lib/modelHealth'

export default function ProjectList({ projects, models = [], onOpen, onCreate, onRename, onDelete }) {
  const [name, setName] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [editName, setEditName] = useState('')
  const defaultModel = models.find((m) => m.default)?.key ?? models[0]?.key ?? ''
  const [model, setModel] = useState('')

  // Project grid sorting: most recently ACTIVE first by default; a second
  // click on the active key flips the direction.
  const [sortKey, setSortKey] = useState('activity') // activity | name | created
  const [sortAsc, setSortAsc] = useState(false)
  const SORTS = [['activity', 'Last activity'], ['name', 'Name'], ['created', 'Created']]
  const sorted = [...projects].sort((a, b) => {
    let cmp
    if (sortKey === 'name') cmp = (a.name ?? '').localeCompare(b.name ?? '')
    else if (sortKey === 'created') cmp = (a.created ?? a.updated) - (b.created ?? b.updated)
    else cmp = a.updated - b.updated
    return sortAsc ? cmp : -cmp
  })
  const pickSort = (key) => {
    if (key === sortKey) setSortAsc((v) => !v)
    else { setSortKey(key); setSortAsc(key === 'name') }
  }

  const openModal = () => {
    setName('')
    setModel(defaultModel)
    setModalOpen(true)
  }

  const create = async () => {
    if (creating) return
    setCreating(true)
    await onCreate(name.trim() || 'Untitled project', model || undefined)
    setName('')
    setCreating(false)
    setModalOpen(false)
  }

  const startRename = (e, p) => {
    e.stopPropagation()
    setEditingId(p.id)
    setEditName(p.name)
  }

  const commitRename = async (p) => {
    if (editingId !== p.id) return
    setEditingId(null)
    const trimmed = editName.trim()
    if (trimmed && trimmed !== p.name) await onRename(p, trimmed)
  }

  return (
    <div className="flex-grow min-h-0 overflow-y-auto p-6">
      <div className="max-w-5xl mx-auto">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="text-lg font-medium text-gray-100">Projects</h2>
            <p className="text-xs font-mono text-gray-500 mt-1">
              each project keeps its own design session — reopen one to resume its context
            </p>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1">
              {SORTS.map(([key, label]) => (
                <button
                  key={key}
                  onClick={() => pickSort(key)}
                  className={`px-2 py-1 rounded text-[10px] font-mono uppercase tracking-wider transition-colors ${
                    sortKey === key
                      ? 'bg-[#161616] border border-gray-700 text-gray-200'
                      : 'border border-transparent text-gray-600 hover:text-gray-400'
                  }`}
                >
                  {label}{sortKey === key ? (sortAsc ? ' ▲' : ' ▼') : ''}
                </button>
              ))}
            </div>
            <Button
              onClick={openModal}
              className="bg-emerald-600 hover:bg-emerald-500 text-white rounded-md px-5 text-sm font-medium"
            >
              New Project
            </Button>
          </div>
        </div>

        {modalOpen && (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
            onClick={() => !creating && setModalOpen(false)}
          >
            <div
              className="bg-[#0d0d0d] border border-gray-800 rounded-lg p-5 w-96 shadow-xl"
              onClick={(e) => e.stopPropagation()}
            >
              <h3 className="text-sm font-medium text-gray-100 mb-1">Create a new project</h3>
              <p className="text-[11px] font-mono text-gray-500 mb-4">
                each project keeps its own design session and history
              </p>
              <input
                autoFocus
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') create()
                  if (e.key === 'Escape' && !creating) setModalOpen(false)
                }}
                placeholder="Project name…"
                maxLength={120}
                className="w-full bg-[#0a0a0a] border border-gray-800 rounded-md px-3 py-2 text-sm font-mono text-gray-200 placeholder-gray-600 focus:outline-none focus:border-emerald-600/60 transition-colors"
              />
              {models.length > 0 && (
                <div className="flex items-center gap-2 mt-3">
                  <ModelHealthDot health={models.find((m) => m.key === model)?.health} />
                  <select
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                    className="w-full bg-[#0a0a0a] border border-gray-800 rounded-md px-3 py-2 text-sm font-mono text-gray-200 focus:outline-none focus:border-emerald-600/60 transition-colors"
                  >
                    {models.map((m) => (
                      <option key={m.key} value={m.key} disabled={!m.available}>
                        {m.display_name}{healthSuffix(m)}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <div className="flex justify-end gap-2 mt-4">
                <Button
                  onClick={() => setModalOpen(false)}
                  disabled={creating}
                  className="bg-transparent border border-gray-700 hover:border-gray-500 text-gray-300 rounded-md px-4 text-sm"
                >
                  Cancel
                </Button>
                <Button
                  onClick={create}
                  disabled={creating}
                  className="bg-emerald-600 hover:bg-emerald-500 text-white rounded-md px-4 text-sm font-medium"
                >
                  {creating ? 'Creating…' : 'Create'}
                </Button>
              </div>
            </div>
          </div>
        )}

        {projects.length === 0 ? (
          <div className="border border-dashed border-gray-800 rounded-lg p-12 text-center">
            <p className="text-gray-500 text-sm font-mono">no projects yet</p>
            <p className="text-gray-600 text-xs mt-2">
              Create one to start a persistent design session.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {sorted.map((p) => (
              <div
                key={p.id}
                onClick={() => editingId !== p.id && onOpen(p)}
                className="group bg-[#0d0d0d] border border-gray-800 rounded-lg p-4 cursor-pointer hover:border-emerald-600/50 transition-colors"
              >
                <div className="flex items-start justify-between gap-2">
                  {editingId === p.id ? (
                    <input
                      autoFocus
                      type="text"
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') commitRename(p)
                        if (e.key === 'Escape') setEditingId(null)
                      }}
                      onBlur={() => commitRename(p)}
                      maxLength={120}
                      className="bg-[#0a0a0a] border border-emerald-600/60 rounded px-1.5 py-0.5 text-sm font-medium text-gray-100 focus:outline-none w-full min-w-0"
                    />
                  ) : (
                    <h3
                      onDoubleClick={(e) => startRename(e, p)}
                      title="Double-click to rename"
                      className="text-sm font-medium text-gray-100 truncate"
                    >
                      {p.name}
                    </h3>
                  )}
                  <div className="flex items-center gap-2 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
                    <button
                      onClick={(e) => startRename(e, p)}
                      className="text-gray-700 hover:text-emerald-400 transition-colors"
                      title="Rename project"
                    >
                      <Pencil className="w-3 h-3" />
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); onDelete(p) }}
                      className="text-gray-700 hover:text-red-400 text-xs font-mono transition-colors"
                      title="Delete project"
                    >
                      ✕
                    </button>
                  </div>
                </div>
                <p className="text-xs text-gray-500 mt-2 line-clamp-2 min-h-[2rem] font-mono">
                  {p.preview || '—'}
                </p>
                <div className="flex items-center justify-between mt-3 text-[10px] font-mono text-gray-600">
                  <span>{p.messages} messages</span>
                  <span>{new Date(p.updated * 1000).toLocaleString()}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
