# Jobs and workbench implementation tracker

Status: PARTS 1–6 FOUNDATION COMPLETE. Created and completed 2026-10-09.

## Purpose

Make the engineering workbench truthful, durable, understandable during a run,
and suitable for multi-file ASIC/FPGA projects. Preserve the existing visual
direction. Never infer evidence that a tool did not produce, and never let a
late REST snapshot overwrite newer WebSocket state.

This file is updated after every completed part with changed files, checks, and
remaining exit criteria.

## Part 1 — Turn-level run progress

Publish and persist run progress at every model-generation and tool boundary,
not only after a complete agent loop. The status popup must distinguish model
generation, reasoning, tool execution, approval pause, terminal state, and a
backend-restart interruption. Token/tool/round budgets must come from the
authoritative run snapshot.

Exit: a browser sees the current round/phase during a live generation; reload
hydrates the same durable snapshot; restart changes an active run to an
explicit interrupted state rather than an empty popup.

## Part 2 — Stable live Jobs registry

Merge REST hydration and WebSocket events by job id with monotonic status and
log sequence handling. Insert a submitted job immediately, update only its row,
retain selection/expansion/scroll state, stream logs while it runs, and clearly
represent queued, running, done, failed, inconclusive, cancelled, interrupted,
and cached-without-job outcomes. Project scope remains deterministic.

Exit: no full-list flash, no missing running row, no terminal-state rewind, and
reconnect resumes logs without duplicates.

## Part 3 — Workbench information architecture

Use stable Sources, Jobs, and Results/Evidence surfaces. Show an unobtrusive
active-job strip without forcibly changing the selected tab. Keep detailed logs
collapsed by default, preserve the user’s chosen tab, and link results back to
the exact job/source/constraint identity.

Exit: source browsing, job monitoring, and evidence inspection do not displace
one another or unexpectedly auto-switch.

## Part 4 — Compact eight-metric summary

Render two balanced rows of four compact cards: cell area, cell count,
utilization, estimated power, setup slack, hold slack, Fmax, and critical-path
delay. Each value must expose its stage/provenance and honest missing, stale,
estimated, screening, or post-layout status. Fmax must never be stranded alone.

Exit: all eight cards fit the workbench width responsively and remain tied to
one selected result snapshot.

## Part 5 — Job details and evidence

Selecting a job reveals tool/flow, design/top, target and constraints, runtime,
outcome, source and constraint digests, logs, metrics, artifacts, failure
summary, and available cancel/replay actions. Evidence links are shown only
when an actual durable record or artifact exists.

Exit: an engineer can answer what ran, on which inputs, with which constraints,
what happened, and where the evidence is stored without reading the raw chat.

## Part 6 — Netlist and physical-view foundation

Add a real-data visualization surface and contracts; do not draw a decorative
or invented schematic. Implement progressively:

1. Netlist/gate-level schematic viewer with module hierarchy and bidirectional
   source-to-module navigation.
2. Critical-path highlighting bound to the exact timing report and netlist.
3. Physical-layout view after P&R, bound to the exact DEF/GDS/layout evidence.

The first delivery may be a durable artifact/index contract and an unavailable
state when no qualified netlist exists. Rendering large graphs must be bounded
and hierarchical rather than loading an entire flattened core into the browser.

Exit: the UI exposes truthful availability and immutable identity for hierarchy,
netlist, timing-path, and layout artifacts; each later renderer can consume the
contract without changing project/job identity semantics.

## Deferred follow-on

- Large-design graph virtualization, cone-of-interest expansion, net tracing,
  and cross-probing between source, timing, schematic, and layout.
- Project export/import must include the selected jobs, evidence records, and
  visualization artifacts with hash verification.
- The engineer CLI/TUI should consume the same job/result/evidence APIs.

## Completion log

### Part 1 — complete

- `backend/agent_core.py` emits bounded authoritative `run_state` snapshots at
  model-round boundaries; `backend/main.py` republishes progress and terminal
  snapshots before `end`.
- Existing restart hydration remains authoritative through
  `project_state.run_state`.

### Part 2 — complete

- `frontend/src/hooks/useAgentSocket.js` rejects frames from superseded sockets,
  preserves monotonic job lifecycle, ordered log cursors, expanded rows, and
  reconnect subscriptions. REST hydration can enrich live data; rewinding it
  lies outside hydration.
- Browser regression covers delayed REST, live logs, reconnect/cancel, replay,
  scroll-follow, project scope, stable rows, and the non-yanking active strip.

### Part 3 — complete

- The workbench now has stable Sources, Jobs, Results, and Visualize surfaces.
  A compact active-job strip exposes new work without changing tabs.
- The host storage path was removed from the UI; the header describes project
  evidence rather than an implementation-specific mount.

### Part 4 — complete

- The responsive summary is eight compact cards in a balanced 4×2 layout:
  area, cells, utilization, estimated power, setup, hold, Fmax, and explicit
  critical delay. Missing values remain unavailable and power remains labeled
  estimated; screening results are not presented as signoff.

### Part 5 — complete

- `GET /api/jobs/{job_id}` exposes durable execution details. Registry rows now
  include start/finish/return/error fields.
- `JobDetails.jsx` shows execution, target/stage, digests, failure information,
  and evidence links. Links are generated only from a manifest-provided run id.

### Part 6 — foundation complete; rich renderers remain follow-on

- `module-index/v1` is bound to the exact hardware-project digest and reports
  declaration-only completeness instead of pretending regex is elaboration.
- The Visualize surface has explicit hierarchy, critical-path, and physical
  layout sections. It consumes only bound module/timing/layout data and shows
  an honest unavailable state otherwise.
- Remaining renderer work: elaborated instance hierarchy and bidirectional
  source navigation; bounded gate/net graph rendering; exact timing-path
  cross-probing; and hash-verified DEF/GDS/ODB physical rendering after P&R.

### Acceptance evidence

- Backend focused suite: 22 passed.
- Frontend lint: 0 errors, 4 pre-existing warnings.
- Frontend production build: passed. The pre-existing chunk-size advisory
  remains.
- Browser suite: 16 passed after the final non-yanking active-strip assertion.
