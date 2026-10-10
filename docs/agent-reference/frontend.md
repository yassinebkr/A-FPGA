# Agent reference — frontend

The frontend uses React 19, Vite 8, Tailwind CSS 4, shadcn/ui, plain JSX, and
the `@/` import alias. The established visual language is a restrained dark
engineering console with `#0a0a0a` background, content-sized status chips,
bounded panels, readable mono evidence, and explicit unavailable/stale states.

## Shell and projects

`App.jsx` owns project-list state, model health, project REST helpers, the
two-column workbench, and synchronous active-project restoration from
`localStorage`. The back action removes the stored project key before clearing
state. Do not move restoration into an effect.

`ProjectList.jsx` supports last-activity/name/created sorting, direction toggle,
create modal, model selection, inline rename, and delete. Unavailable models
remain visible with disabled selection and a health dot.

## Socket, chat, and status

`useAgentSocket.js` handles reconnect, frame parsing, history/project-state
hydration, status heartbeats, provider reasoning, token usage, tool verdicts,
job broadcasts, live logs, replay cursors, cancellation, and project filtering.
It subscribes immediately on `job_started`; a later `job_id` marker remains a
fallback. Finished jobs can be expanded for disk/ring replay. Job entries are
tagged with their owning session and use persisted sequence cursors.

`AgentChat.jsx` renders styled Markdown/GFM, user and assistant messages,
tool-call/result rows, status text, and a folded reasoning tile. Reasoning is
live-only in persisted history and replayed for a mid-run rejoin. The tile
supports hover/pin, outside dismissal, Escape, viewport fitting, downward
expansion where space permits, scroll-follow pause/resume, and reduced-motion
behavior. The Markdown renderer is a deferred chunk.

## Workbench surfaces

`LiveStateDashboard.jsx` owns the eight metric band, activity/results, Sources,
Jobs, and Visualize surfaces. Metrics stay content-sized and balanced in a
4×2 grid at wide widths, then 2×4 and 1×8 as container width requires.
Missing values preserve honest assurance text. Derived critical delay and
Fmax conversions use the same parser for live and restored state.

Jobs show project-scoped live-first rows, status chips, cancel controls,
bounded log buffers, log-gap markers, runtime stamps, and auto-scroll that
pauses when the user moves away from the bottom. Visualize and Results prefer
the newest synthesis/Fmax job carrying the relevant evidence.

## Sources and visualization

Sources select each authoritative file independently and preserve file metadata.
`VerilogView.jsx` provides regex syntax highlighting without adding a parser
dependency.

Gate schematic and physical layout are lazy-loaded after the first Visualize
visit. `GateSchematic.jsx` delegates artifact loading, bounded graph/timing
state, worker layout, viewport controls, glyphs, toolbar, timing, canvas, and
details to focused modules. `PhysicalLayout.jsx` delegates REST state and
viewport behavior to `usePhysicalLayout.js` and focused presentation modules.

Every visual surface is bound to project, job, source, constraint, tool,
netlist, and artifact digests. The browser receives bounded graph/layout data;
it never invents gates, hierarchy, paths, or physical placement. Stale,
partial, truncated, black-box, incompatible, missing, and screening-unavailable
states remain visible. Selection cross-probes between schematic, timing,
physical layout, Sources, and the selected job. Worker teardown and stale
response guards are mandatory.

## Frontend validation and style

```bash
cd frontend
npm run lint
npm run build
npm run test:browser
npm run test:browser:perf
```

Browser tests use mocked backend routes and Playwright. CI disables reuse of
an existing Vite server; remove orphaned `vite --port 4173` processes before
reruns. The agent shell exports `CI=true`, which forces that CI behavior. The
C5 large-netlist tests measure bounded 500/1,000-node transfer, heap, long
tasks, zoom, cleanup, and superseded request handling. The normal browser suite
excludes the isolated `@perf` case; `test:browser:perf` runs it with one worker.
The controlled memoized SVG/minimap run closes the post-load 100 ms interaction
budget: zoom was 48 ms and the maximum post-load long task was 58 ms. The
Five load-phase long tasks were recorded, with 296 ms as the maximum; they
remain separately reported as startup evidence.

Use functional JSX components and hooks, Tailwind utilities, shadcn/ui
components, and the existing dark palette. Keep public props, test IDs, and
the GenUI app-owned protocol stable unless a migration is documented. The
frontend build receives `VITE_BACKEND_PORT=8001` in Docker and defaults to
port 8000 for host development. Shared shadcn `buttonVariants` exports and
Fast Refresh boundaries stay compatible with the lint policy.
