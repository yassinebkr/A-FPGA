import { useCallback, useEffect, useState } from 'react'
import AgentChat from './components/AgentChat'
import LiveStateDashboard from './components/LiveStateDashboard'
import ModelHealthDot from './components/ModelHealthDot'
import ProjectList from './components/ProjectList'
import ProjectOverview from './components/ProjectOverview'
import MistralSetupModal from './components/MistralSetupModal'
import { healthSuffix } from './lib/modelHealth'
import {
  useAgentSocket, fetchProjects, fetchModels, createProject, renameProject,
  setProjectModel, setProjectEffort, deleteProject,
} from './hooks/useAgentSocket'

const ACTIVE_PROJECT_KEY = 'agent:activeProject'

const STATUS_STYLE = {
  open: 'bg-emerald-500',
  connecting: 'bg-amber-400 animate-pulse',
  closed: 'bg-red-500',
}
const STATUS_LABEL = { open: 'Connected', connecting: 'Connecting', closed: 'Reconnecting' }

function App() {
  // Restore the last opened project synchronously on mount. Doing this in a
  // state initializer (not an effect) matters: an effect-based restore races
  // the persist effect below, which would wipe the stored id on the first
  // null render before the project list finishes loading.
  const [project, setProject] = useState(() => {
    try {
      const stored = JSON.parse(localStorage.getItem(ACTIVE_PROJECT_KEY))
      return stored && typeof stored.id === 'string' ? stored : null
    } catch {
      return null
    }
  }) // { id, name } | null
  const [projects, setProjects] = useState([])
  const [mistralSetupOpen, setMistralSetupOpen] = useState(false)
  const [models, setModels] = useState([])
  const { status, model, messages, busy, isRunning, statusText, runState, generation, reasoning, timeline, metrics, metricProvenance, verilog, latestLint, hardwareProject, designContract, contractActionPending, activity, jobs, jobsState, send, sendCancel, sendContractAction, subscribeJob, cancelJob } =
    useAgentSocket(project?.id ?? null)

  const refreshProjects = useCallback(async () => {
    setProjects(await fetchProjects())
  }, [])

  useEffect(() => { refreshProjects() }, [refreshProjects])

  // Poll the model registry (with health statuses) on mount and every 60 s;
  // the backend caches probes, so this is cheap.
  useEffect(() => {
    let cancelled = false
    const load = () => fetchModels().then((m) => { if (!cancelled) setModels(m) }).catch(() => {})
    load()
    const timer = setInterval(load, 60000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [])

  // The current project's model key comes from the (refreshed) project list,
  // so a PATCH via the header select is reflected without extra state.
  const activeModel = projects.find((p) => p.id === project?.id)?.model ?? null

  // Persist the active project (id + name, so restore needs no list fetch).
  useEffect(() => {
    if (project) {
      localStorage.setItem(ACTIVE_PROJECT_KEY, JSON.stringify({ id: project.id, name: project.name }))
    } else {
      localStorage.removeItem(ACTIVE_PROJECT_KEY)
    }
  }, [project])

  const handleCreate = async (name, model) => {
    const p = await createProject(name, model)
    await refreshProjects()
    if (p) setProject(p)
  }

  const handleModelChange = async (key) => {
    if (!project || !key || key === activeModel) return
    await setProjectModel(project.id, key)
    await refreshProjects()
  }

  const handleEffortChange = async (effort) => {
    if (!project) return
    // '' clears the project setting back to the model's registry default.
    await setProjectEffort(project.id, effort || null)
    await refreshProjects()
  }

  const handleDelete = async (p) => {
    if (!window.confirm(`Delete project "${p.name}"? Its chat history will be lost.`)) return
    await deleteProject(p.id)
    if (project?.id === p.id) setProject(null)
    await refreshProjects()
  }

  const handleRename = async (p, name) => {
    const updated = await renameProject(p.id, name)
    if (updated && project?.id === p.id) setProject((cur) => ({ ...cur, name: updated.name }))
    await refreshProjects()
  }

  const handleBack = async () => {
    // Remove the stored id BEFORE clearing state: the restore effect runs
    // before the persist effect on the re-render, so if the key is still
    // there it instantly re-opens the project (the "PROJECTS does nothing" bug).
    localStorage.removeItem(ACTIVE_PROJECT_KEY)
    setProject(null)
    await refreshProjects()
  }

  return (
    <div className="w-full h-screen bg-[#0a0a0a] text-gray-200 flex flex-col font-sans overflow-hidden">
      <header className="shrink-0 flex items-center justify-between px-5 py-3 border-b border-gray-800 bg-[#0d0d0d]">
        <div className="flex items-center gap-3">
          <div
            className="w-7 h-7 rounded bg-emerald-500/10 border border-emerald-500/40 flex items-center justify-center cursor-pointer"
            onClick={handleBack}
            title="All projects"
          >
            <svg className="w-4 h-4 text-emerald-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          </div>
          <div>
            <h1 className="text-sm font-medium tracking-tight text-gray-100">
              Autonomous Hardware Agent
            </h1>
            <p className="text-[10px] font-mono text-gray-500">
              {project ? project.name : 'FPGA / ASIC design workbench'}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {project && (
            <button
              onClick={handleBack}
              className="px-2.5 py-1 bg-[#161616] border border-gray-800 rounded text-[10px] text-gray-400 font-mono uppercase tracking-wider hover:border-gray-600 transition-colors"
            >
              ← Projects
            </button>
          )}
          {project && models.length > 0 && (
            <div className="flex items-center gap-1">
              <div className="flex items-center gap-1 px-2 py-1 bg-[#161616] border border-gray-800 rounded">
                <ModelHealthDot health={models.find((m) => m.key === activeModel)?.health} />
                <select
                  value={activeModel ?? ''}
                  onChange={(e) => handleModelChange(e.target.value)}
                  title="Model for this project"
                  aria-label="Project model"
                  className="bg-transparent text-[10px] text-gray-400 font-mono focus:outline-none cursor-pointer"
                >
                  {models.map((m) => (
                    <option key={m.key} value={m.key} disabled={!m.available && m.key !== activeModel}>
                      {m.display_name}{healthSuffix(m)}
                    </option>
                  ))}
                </select>
              </div>
              {(() => {
                // Effort selector, only for models that declare effort levels
                // (GLM 5.3). '' = the registry default for the model.
                const entry = models.find((m) => m.key === activeModel)
                if (!entry?.reasoning_efforts?.length) return null
                const current = projects.find((p) => p.id === project?.id)?.reasoning_effort ?? ''
                return (
                  <div className="px-2 py-1 bg-[#161616] border border-gray-800 rounded">
                    <select
                      value={current}
                      onChange={(e) => handleEffortChange(e.target.value)}
                      title="Reasoning effort for this project (scales the run token budget)"
                      aria-label="Reasoning effort"
                      className="bg-transparent text-[10px] text-gray-400 font-mono focus:outline-none cursor-pointer"
                    >
                      <option value="">
                        effort: auto{entry.reasoning_effort_default ? ` (${entry.reasoning_effort_default})` : ''}
                      </option>
                      {entry.reasoning_efforts.map((effort) => (
                        <option key={effort} value={effort}>effort: {effort}</option>
                      ))}
                    </select>
                  </div>
                )
              })()}
              {models.some((m) => m.key.startsWith('mistral') && !m.available) && (
                <button
                  type="button"
                  onClick={() => setMistralSetupOpen(true)}
                  className="rounded border border-emerald-500/40 px-1.5 py-0.5 text-[8px] uppercase tracking-wider text-emerald-300 hover:bg-emerald-500/10"
                  title="Set up the Mistral provider: paste a key or sign in with your browser (vibe --setup flow)"
                  data-testid="mistral-setup-button"
                >
                  mistral setup
                </button>
              )}
            </div>
          )}
          {model && !project && (
            <span className="px-2 py-1 bg-[#161616] border border-gray-800 rounded text-[10px] text-gray-400 font-mono">
              {model}
            </span>
          )}
          <div className="flex items-center gap-2 px-2.5 py-1 bg-[#161616] border border-gray-800 rounded">
            <span className={`w-2 h-2 rounded-full ${STATUS_STYLE[status]}`} />
            <span className="text-gray-400 text-[10px] font-mono uppercase tracking-wider">
              {STATUS_LABEL[status]}
            </span>
          </div>
        </div>
      </header>

      {project ? (
        <>
          <ProjectOverview
            project={project}
            activeModel={activeModel}
            verilog={verilog}
            latestLint={latestLint}
            hardwareProject={hardwareProject}
            designContract={designContract}
            metrics={metrics}
            metricProvenance={metricProvenance}
            jobs={jobs}
          />
        <main className="flex-grow min-h-0 grid grid-cols-1 lg:grid-cols-12 gap-3 p-3">
          <div className="lg:col-span-5 min-h-0">
            <AgentChat
              messages={messages}
              busy={busy}
              isRunning={isRunning}
              statusText={statusText}
              runState={runState}
              generation={generation}
              reasoning={reasoning}
              timeline={timeline}
              designContract={designContract}
              metrics={metrics}
              contractActionPending={contractActionPending}
              onContractAction={sendContractAction}
              connected={status === 'open'}
              onSend={send}
              onCancel={sendCancel}
            />
          </div>
          <div className="lg:col-span-7 min-h-0">
            <LiveStateDashboard
              metrics={metrics}
              metricProvenance={metricProvenance}
              verilog={verilog}
              hardwareProject={hardwareProject}
              activity={activity}
              jobsState={jobsState}
              jobs={jobs}
              projectId={project.id}
              onCancelJob={cancelJob}
              onSubscribeJob={subscribeJob}
            />
          </div>
        </main>
        </>
      ) : (
        <ProjectList
          projects={projects}
          models={models}
          onOpen={setProject}
          onCreate={handleCreate}
          onRename={handleRename}
          onDelete={handleDelete}
        />
      )}
      {mistralSetupOpen && <MistralSetupModal onClose={() => setMistralSetupOpen(false)} />}
    </div>
  )
}

export default App
