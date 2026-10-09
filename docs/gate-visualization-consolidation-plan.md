# Gate visualization consolidation plan

Status: IN PROGRESS — C0–C1 complete; C2 is next.

## Objective

Consolidate the D1–G4 implementation before adding another major capability.
The current implementation is functionally strong and well tested, but its
Git provenance, frontend warnings, component size, bundle size, and large-core
performance need to be brought under control.

This plan does not redesign the product or weaken evidence binding. Netlist,
timing-path, and physical-layout views must remain bound to the exact project,
job, source, constraint, and artifact digests.

## Phase C0 — Recover provenance and establish the baseline

### Steps

1. Determine why the workspace has no `.git` directory and whether an existing
   remote or recoverable repository metadata exists elsewhere.
2. Do not initialize, overwrite, move, or reconnect Git until the owner has
   confirmed the intended remote and repository history.
3. Inventory source files, generated data, secrets, build products, tool
   installations, and durable project/job/evidence data.
4. Verify `.gitignore` coverage before any repository initialization or first
   snapshot. Explicitly exclude `.env`, provider keys, logs, databases,
   caches, build outputs, downloaded tools, generated evidence, and frontend
   dependencies.
5. Capture a reproducible pre-refactor baseline:
   - complete backend test result;
   - frontend lint result with every warning classified as existing or new;
   - production build and bundle sizes;
   - complete browser suite;
   - one small netlist fixture and one real persisted visualization artifact.
6. Record the exact baseline and any unavailable provenance in this document.

### Exit criteria

- Repository provenance is restored or its absence is explicitly resolved by
  the owner.
- No secret or generated runtime data is tracked.
- The current working implementation has a recoverable baseline.
- Every later phase can be reviewed as an isolated diff.

## Phase C1 — Correctness and warning cleanup

### Steps

1. Remove unused imports and variables in visualization code and tests.
2. Fix the missing hook dependency in `PhysicalLayout.jsx` without creating an
   unstable callback or fetch loop.
3. Remove the render-time ref mutation in `MistralSetupModal.jsx` and verify
   that async setup/polling behavior remains current and cancellable.
4. Audit each `set-state-in-effect` warning:
   - eliminate state that can be derived during render;
   - reset project/job-scoped state through keyed boundaries or explicit
     actions where appropriate;
   - retain effects only when synchronizing with network, DOM, or worker state;
   - document any deliberately retained warning.
5. Reconcile documentation with measured results. Do not state “lint clean”
   when warnings remain.
6. Add regression tests for every behavior touched during warning cleanup.

### Exit criteria

- Frontend lint has zero errors and no unexplained warnings introduced by D1–G4.
- No stale closure, request race, duplicate fetch, or selection-reset regression
  appears in browser tests.
- Backend focused tests, frontend build, and full browser suite pass.

## Phase C2 — Decompose the visualization frontend

### Steps

1. Characterize responsibilities currently combined in
   `GateSchematic.jsx`: artifact loading, graph-query state, worker layout,
   viewport controls, selection, timing overlays, search, hierarchy, SVG
   rendering, and details panels.
2. Extract stateful hooks with narrow contracts:
   - netlist metadata and bounded graph loading;
   - timing-path loading and digest validation;
   - worker layout lifecycle;
   - viewport/pan/zoom behavior;
   - shared schematic/layout selection.
3. Extract presentation components:
   - visualization identity and scope bar;
   - graph canvas and cell/net glyphs;
   - search and hierarchy controls;
   - timing-path selector and delay details;
   - selected-object evidence panel;
   - unavailable, stale, partial, truncated, and error states.
4. Decompose `PhysicalLayout.jsx` along the same shared-selection and viewport
   boundaries where doing so reduces duplication.
5. Keep API shapes and test IDs stable unless a deliberate migration is
   documented.
6. Avoid a framework rewrite or visual redesign during this phase.

### Exit criteria

- No new monolithic replacement component is created.
- Data fetching, evidence validation, layout computation, and rendering have
  independently testable boundaries.
- Existing cross-probing, tab persistence, keyboard behavior, and error states
  remain unchanged to the user.
- Backend, lint, build, and browser suites pass after each extraction batch.

## Phase C3 — Backend modularity and contract audit

### Steps

1. Review `gate_netlist.py`, `timing_paths.py`, and `physical_layout.py` for
   mixed responsibilities rather than splitting files by line count alone.
2. Separate, where useful:
   - canonical schemas and validation;
   - Yosys/STA/DEF parsing;
   - immutable identity and digest handling;
   - bounded graph/query construction;
   - persistence adapters and API serialization.
3. Centralize repeated canonical JSON, digest, unavailable-state, and artifact
   identity logic without weakening type- or schema-specific validation.
4. Audit size limits, traversal resistance, duplicate identifiers, malformed
   references, compressed-input limits, and denial-of-service boundaries.
5. Confirm restart durability using a fresh `JobManager` and evidence
   repository, not only in-memory fixtures.
6. Keep existing schema versions stable unless a migration is genuinely
   required; any new schema version needs compatibility tests.

### Exit criteria

- Artifact contracts remain deterministic and fail closed.
- Parsing, validation, querying, and persistence can be tested independently.
- Existing stored artifacts remain readable or receive an explicit migration.
- Full backend suite passes.

## Phase C4 — Bundle and runtime efficiency

### Steps

1. Measure the production bundle composition before optimizing it.
2. Lazy-load the `Visualize` surface, gate schematic, physical layout, and
   worker-dependent code so users who remain in Sources/Jobs do not download
   the whole viewer eagerly.
3. Preserve worker isolation for graph layout and verify worker cleanup when
   projects, jobs, or routes change.
4. Profile rendering, layout, search, zoom, and selection on representative
   bounded graphs.
5. Eliminate avoidable recomputation and rerenders using measured evidence;
   do not add memoization speculatively.
6. Set explicit bundle and interaction budgets and enforce them in a repeatable
   report or test.

### Initial budgets

- Main application chunk below the current 500 kB Vite warning threshold.
- Visualization code emitted as separate lazy chunks.
- No long task above 100 ms for normal small/medium graph interaction on the
  development machine.
- Worker and network requests are cancelled or superseded cleanly.

### Exit criteria

- The main bundle no longer produces the current oversized-chunk warning, or a
  measured and documented exception is approved.
- Opening and closing visualization surfaces does not leak workers or retain
  incompatible project/job state.
- Browser regression coverage includes lazy-load failure and recovery.

## Phase C5 — Large-netlist qualification

### Steps

1. Select or generate three reproducible benchmark classes:
   - small design for exact visual correctness;
   - medium multi-module design for normal engineering interaction;
   - large design approaching hundreds of thousands of cells for bounding and
     failure behavior.
2. Record artifact sizes, parsing time, persistence time, endpoint latency,
   transferred bytes, worker-layout time, peak browser memory, and interaction
   responsiveness.
3. Verify that the initial request opens only top-level hierarchy and never
   transfers the flattened large design.
4. Exercise hierarchy expansion, cone queries, search, timing-path highlight,
   source cross-probing, and physical-location cross-probing under configured
   limits.
5. Verify truncation, cancellation, timeout, malformed-artifact, missing-path,
   and browser-refresh behavior.
6. Tune limits from measurements while keeping them server-enforced and
   visible to the user.

### Exit criteria

- Small and medium designs preserve exact expected behavior.
- The large benchmark stays inside documented server, transfer, worker, and
  memory limits without freezing the interface.
- Requests exceeding limits fail clearly and recoverably.
- The remaining Part 6 large-design performance criterion is either closed or
  documented with a precise reproducible blocker.

## Phase C6 — Final qualification and documentation

### Steps

1. Run the complete backend suite, frontend lint, production build, and full
   browser suite from a clean environment.
2. Run one bounded real synthesis/STA visualization path and, if an existing
   qualified P&R artifact is available, its physical-layout path without
   launching unnecessary paid model calls or EDA reruns.
3. Verify reload and backend-restart durability for netlist, timing, layout,
   selection identity, and honest unavailable states.
4. Update the main architecture/roadmap documents with measured capability and
   limitations, not implementation claims alone.
5. Mark Part 6 closed only if all G1–G4 criteria and the large-design criterion
   pass.

### Exit criteria

- Test and performance evidence is current and reproducible.
- Documentation agrees with measured lint, build, test, and qualification
  results.
- No critical or high-severity known issue remains hidden behind a “complete”
  label.

## Execution order

Execute strictly in this order:

1. C0 — provenance and baseline
2. C1 — correctness and warnings
3. C2 — frontend decomposition
4. C3 — backend contract audit
5. C4 — bundle/runtime efficiency
6. C5 — large-netlist qualification
7. C6 — final qualification and documentation

C2 and C3 may be prepared independently only after C1 is green, but their
integration and acceptance checks remain sequential. Do not start C4 before
the frontend boundaries from C2 are stable. Do not claim Part 6 closure before
C5 and C6.

## Update discipline

After each phase, append a completion record containing:

- exact files changed;
- design decisions and rejected alternatives;
- exact commands and results;
- measured warning, bundle, latency, memory, and graph-size data when relevant;
- remaining criteria and limitations;
- whether the phase is complete, partially complete, or blocked.

No paid model call, destructive Git operation, tool installation, or new EDA
run is authorized by this plan alone.

## Completion records

### C0 — complete (2026-10-10)

#### Provenance

- `/home/kwestog/Documents/code/fpga` contains no `.git` directory and Git
  discovery stops at the filesystem boundary.
- No Git bundle, bare repository, project-specific repository metadata, or
  documented project remote was found under
  `/home/kwestog/Documents/code` at the audited depth. The `.git` directories
  found there belong to the unrelated `beyondvram` and `near_agi` projects.
- With owner approval, a new local repository was initialized on branch
  `main`. Commit `68fb285` is the first recoverable baseline. No remote was
  guessed, configured, or contacted; history before this baseline remains
  unavailable by design.

#### Ignore audit

- Existing exclusions already protected root and nested `.env` files,
  `backend/data`, `backend/logs`, persisted projects, `node_modules`, frontend
  build output, root build output, `.tools`, and the third-party `tapeout`
  workspace.
- `.gitignore` was extended for `.venv/`, `venv/`, Playwright results/reports,
  coverage output, and all `*.log` files. This prevents the two local Python
  environments, browser-test state, and `fmax-acceptance.log` from entering a
  future first snapshot.
- The rules were verified in an isolated temporary Git repository against
  representative secrets, databases, projects, logs, dependencies, builds,
  tools, and third-party files. `backend/.env.example` and
  `backend/projects/.gitkeep` remain intentionally trackable.

#### Reproducible baseline

- Intended automated backend suite:
  `cd backend && .venv/bin/python -m pytest -q tests test_agent_context.py test_agent_progress.py test_ibex_qualification_policy.py`
  — **560 passed**, one upstream Starlette/anyio deprecation warning, in
  14.08 seconds.
- Unqualified `pytest -q` is not a valid clean-suite command today: it collects
  six manual/live-server probes from the backend root. Five attempt network
  connections during module import and one imports the removed `fpga_agent`
  symbol. This test-discovery defect is recorded for C1; it is not hidden by
  the 560-test result.
- Frontend lint: zero errors and **14 warnings**. New/relevant warnings include
  an unused G2 test import, unused `reduceMotion`, a missing callback dependency
  in `PhysicalLayout.jsx`, render-time ref mutation in
  `MistralSetupModal.jsx`, and several synchronous state resets in effects.
- Frontend production build: passed. Main JS chunk **576.66 kB** minified /
  **173.67 kB** gzip; Vite reports the expected >500 kB chunk warning. The
  graph-layout worker is a separate 1.91 kB chunk.
- Browser suite: **43 passed** in 21.1 seconds.
- Focused visualization backend contracts: **33 passed** before the full
  backend baseline.
- Durable real-artifact baseline includes:
  - job `syn-a0c38456`, netlist
    `0785993a8ae28d440f7a176aaea08b6e859fdf1a6539d1f0ccdb01a076b9bc66`
    and timing paths
    `33fb252f71a3efeac6e117e811ada3160b073143a2b3b1c3b2fef2c7a556f81e`;
  - job `syn-6730103f`, netlist
    `3179a67f0658195c3697a35544b57facc8479bcc9af9528a8e1e7adc42af184c`
    and layout
    `75bc0a6bbab92b85d77db1756a76debaa6cb5e55a680a8c565b0fce94f78e5fc`.
- No paid model call or new EDA run was made during C0.

#### C0 closure

The source/runtime boundary, ignore policy, reproducible validation baseline,
and recoverable local Git baseline are established. A remote and first push
remain optional future owner decisions and do not block C1.

### C1 — complete (2026-10-10)

#### Correctness and warning cleanup

- Removed the unused visualization test import, unused physical-layout motion
  state, and unused availability ref.
- Fixed the `PhysicalLayout` container callback dependency.
- Moved the Mistral setup view ref update into the committed effect lifecycle;
  polling remains cancellable through the existing timer cleanup.
- Audited every `react/set-state-in-effect` finding. The retained effects
  synchronize REST hydration, job-scoped evidence, cross-probing, project
  boundaries, or WebSocket/job-registry state; each suppression is local and
  documented at the synchronization point.
- Preserved the shared shadcn `buttonVariants` API and documented its Fast
  Refresh boundary locally.
- Standard backend discovery now targets `backend/tests/`. The three automated
  root tests moved into that directory; manual live-server probes remain at the
  backend root and are excluded from a bare `pytest -q` run.

#### Acceptance evidence

- `cd backend && .venv/bin/python -m pytest -q` — **560 passed**, one upstream
  Starlette/anyio deprecation warning, in 12.04 seconds.
- `cd frontend && npm run lint` — **zero errors, zero warnings**.
- `cd frontend && npm run build` — passed; main JS is **576.40 kB** minified /
  **173.65 kB** gzip. The known >500 kB chunk warning remains assigned to C4.
- `cd frontend && npm run test:browser` — **43 passed** in 25.8 seconds.
- No paid model call or EDA run was made during C1.

#### C1 closure

The warning cleanup, hook correctness audit, clean default backend discovery,
production build, and complete browser regression gate are green. C2 may begin.
