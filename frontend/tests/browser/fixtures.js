
export const PROJECTS = [
  { id: 'proj-a', name: 'Alpha', preview: '', messages: 0, created: 2, updated: 20, model: 'mock' },
  { id: 'proj-b', name: 'Beta', preview: '', messages: 0, created: 1, updated: 10, model: 'mock' },
]

export function installMockBackend(page, { delayedJobs = false } = {}) {
  let releaseJobs
  const jobsReady = new Promise((resolve) => { releaseJobs = resolve })
  const sockets = []
  const sent = []

  page.route('http://127.0.0.1:8000/health', (route) =>
    route.fulfill({ json: { model: 'mock', api_key_set: true } }))
  page.route('http://127.0.0.1:8000/api/models', (route) =>
    route.fulfill({ json: [{ key: 'mock', display_name: 'Mock', available: true, default: true, health: 'ok' }] }))
  page.route('http://127.0.0.1:8000/api/projects', (route) =>
    route.fulfill({ json: PROJECTS }))
  page.route('http://127.0.0.1:8000/api/jobs', async (route) => {
    if (delayedJobs) await jobsReady
    await route.fulfill({ json: [{ id: 'job-live', kind: 'simulation', design: 'matmul3', status: 'queued', session: 'proj-a', created_at: new Date(1_000).toISOString() }] })
  })
  page.route(/http:\/\/127\.0\.0\.1:8000\/api\/jobs\/[^/]+$/, (route) => {
    const id = route.request().url().split('/').at(-1)
    return route.fulfill({ json: { id, kind: 'simulation', design: 'matmul3', status: 'running', session: 'proj-a', params: {} } })
  })

  page.addInitScript(() => {
    window.__mockSockets = []
    window.WebSocket = class MockWebSocket {
      static OPEN = 1
      constructor() { this.readyState = 1; window.__mockSockets.push(this); setTimeout(() => this.onopen?.(), 0) }
      send(raw) {
        const frame = JSON.parse(raw)
        window.__mockSent = [...(window.__mockSent || []), frame]
        if (frame.type === "cancel_job") setTimeout(() => this.receive({ type: "job_status", job_id: frame.job_id, status: "cancelled" }), 0)
        if (frame.type === "subscribe" && frame.job_id === "job-live") {
          setTimeout(() => this.receive({ type: "tool_log", job_id: "job-live", seq: 1, line: "compile started" }), 0)
          setTimeout(() => this.receive({ type: "tool_log", job_id: "job-live", seq: 2, line: "simulate running" }), 0)
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }) }
      close() { this.readyState = 3; this.onclose?.({ code: 1001 }) }
    }
  })
  const emit = async (frame, socketIndex = -1) => page.evaluate(([item, index]) => window.__mockSockets.at(index)?.receive(item), [frame, socketIndex])

  return {
    sockets,
    sent,
    wasSent: (type, jobId) => page.evaluate(([t, id]) => (window.__mockSent || []).some((f) => f.type === t && f.job_id === id), [type, jobId]),
    send: emit,
    releaseJobs,
    async jobStarted({ id = 'job-live', session = 'proj-a', status = 'running' } = {}) {
      await emit({ type: "job_started", job_id: id, kind: "simulation", design: "matmul3", session })
      await emit({ type: "job_status", job_id: id, status })
    },
    async log(id = 'job-live', seq = 3, line = `log ${seq}`) {
      await emit({ type: "tool_log", job_id: id, seq, line })
    },
    async result(text = 'simulation passed\ncached: true') {
      await emit({ type: "tool_result", content: text })
    },
    async closeSocket() { await page.evaluate(() => window.__mockSockets.at(-1)?.close()) },
    async finish(id = "job-live") { await emit({ type: "job_status", job_id: id, status: "done" }) },
    async frame(frame) { await emit(frame) },
    async reasoningStart({ generationId = 'gen-1', round = 1 } = {}) {
      await emit({ type: 'generation_start', generation_id: generationId, round })
    },
    async reasoning(text, { generationId = 'gen-1', round = 1 } = {}) {
      await emit({ type: 'reasoning_delta', generation_id: generationId, round, content: text })
    },
    async text(text, { generationId = 'gen-1', round = 1 } = {}) {
      await emit({ type: 'text_delta', generation_id: generationId, round, content: text })
    },
    async reasoningEnd({ generationId = 'gen-1', round = 1 } = {}) {
      await emit({ type: 'generation_end', generation_id: generationId, round })
    },
  }
}
