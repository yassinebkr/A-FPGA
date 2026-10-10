# Workbench density and gate-level visualization plan

Status: D1, D2, G1, G2, G3, G4 implemented, checked, and live-validated;
large-design bounded qualification is complete, while the interaction budget
remains open (2026-10-10). KLayout was installed into the user nix profile via
`scripts/install-klayout.sh` and a real pnr flow closed Part 6 item 4.

This plan follows the completed foundation in
[`jobs-workbench-implementation-plan.md`](jobs-workbench-implementation-plan.md).
It has two goals: recover vertical workspace from the project overview and
metric summary, then finish Part 6 with a real, evidence-bound gate-level
viewer. The existing dark engineering-console visual direction remains the
baseline.

## Scope and invariants

- Do not change qualification semantics or present screening evidence as
  signoff.
- Do not invent gates, hierarchy, paths, or layout information when artifacts
  are absent.
- Every visualization is bound to the selected project, source digest, job,
  constraints, tool versions, netlist digest, and artifact digest.
- Large designs are opened hierarchically and incrementally; the browser never
  receives an unbounded flattened graph by default.
- Sources, Jobs, Results, and Visualize remain stable workbench surfaces.

## Phase D1 — Compact qualification strip

Replace the eleven large qualification tiles below the project header with
content-sized status chips in the header row, placed between the `not
qualified` badge and the `next: Contract` indicator.

Each chip contains only a status dot and the short gate label (`Contract`,
`Sources`, `Lint`, and so on). Its width is determined by its contents; it must
not use `flex: 1` or a fixed/minimum width. Evidence, exact state, and next
action remain accessible through a keyboard-accessible hover/focus/click
popover. The popover must close on Escape, outside click, or selecting another
gate.

Responsive behavior:

- Wide workbench: all chips stay on the same header line when they fit.
- Medium width: the chip strip becomes a single-line horizontal scroller; it
  must not increase the header height.
- Narrow/mobile width: project identity occupies the first compact line and
  the chips occupy one second line. No chip text is truncated ambiguously.
- The failed/running/awaiting gate remains visually distinguishable without
  relying on color alone; the accessible label includes the full state.

Acceptance:

- At the screenshot's desktop width, project identity, all gate chips, and the
  next-action indicator fit in one compact band.
- The project overview no longer dedicates a full row of tall cards to gates.
- Existing gate state, evidence, and next-action tests continue to pass.
- Add browser coverage for keyboard access, outside-click dismissal, overflow,
  and the wide single-line layout.

## Phase D2 — Dense synthesis metric band

Reduce the vertical footprint of the eight workbench metrics while preserving
legibility and provenance.

Use a four-column by two-row grid at normal desktop workbench widths. Cards
should be content-driven, with approximately `44–48px` total height rather than
the current `64px` minimum. Tighten padding, label/value spacing, and value line
height; keep the number visually dominant within a restrained size. Assurance text
(`screening`, `estimated`, `unavailable`) becomes a compact badge or tooltip so
it cannot force card height.

Responsive behavior remains container-based:

- Four columns when the workbench can display the values safely.
- Two columns at medium widths.
- One column only when genuinely necessary.
- Long or missing values must not stretch one card and therefore the whole row.

Acceptance:

- Eight metrics remain balanced as two rows of four at the screenshot width.
- The metric band is materially shorter with no clipped labels, values, units,
  provenance, focus rings, or tooltips.
- Fmax and critical delay remain visible in the second row, never stranded.
- Browser snapshots cover empty, populated, unusually long, and negative
  timing values at wide and narrow container widths.

## Phase G1 — Durable synthesized-netlist artifact

Extend the completed Part 6 foundation with a canonical gate-netlist artifact
produced by a successful synthesis job. Prefer the synthesis tool's structured
JSON output (for example Yosys JSON) over reparsing emitted Verilog.

The artifact contract must include:

- schema version and artifact SHA-256;
- project, source, constraint, selected-job, and synthesized-netlist digests;
- top module and module/cell hierarchy;
- cell type, stable cell id, ports, pin directions, parameters, and source
  attributes when supplied by the tool;
- nets and driver/load pin references;
- tool and library identity;
- explicit completeness, truncation, unsupported-primitive, and black-box
  fields.

Persist it through the existing evidence/artifact store, expose metadata via
job details, and add a bounded graph endpoint that accepts module, hierarchy
scope, depth, node limit, and optional cone root. Reject stale or mismatched
digests rather than silently combining artifacts.

Acceptance:

- A deterministic fixture produces byte-stable canonical output and digest.
- Invalid pin/net references, duplicate ids, unsupported schema versions, path
  traversal, and oversized requests fail safely.
- Restarting the backend does not lose artifact availability or identity.
- A job without structured synthesis output reports `unavailable`; no diagram
  is fabricated from the RTL declaration index.

## Phase G2 — Hierarchical gate-level schematic viewer

Replace the current textual visualization placeholder with a bounded schematic
inside the existing `Visualize` surface.

The first useful view shows the selected module or hierarchy scope, recognizable
standard cells/logic primitives, named ports, directed nets, fanout, and
hierarchy boundaries. Start collapsed at the top module and expand modules or a
selected cone on demand. Use a deterministic layered layout in a worker and
render only the visible/bounded graph; layout work must not block chat, job
streaming, or cancellation.

Required interaction:

- pan, zoom, fit, reset, and minimap/overview for non-trivial graphs;
- search by module, instance, cell, signal, or source location;
- select a node/net to inspect exact properties and provenance;
- expand/collapse hierarchy and fan-in/fan-out cones with visible node limits;
- navigate module/cell source attributes into the Sources view and return to
  the same graph selection;
- clear empty, stale, truncated, black-box, loading, and error states.

The visual language should resemble an engineering schematic and avoid the
generic force-directed bubble look. Cell glyphs, wires, selection, and status colors
must follow the existing restrained console design.

Acceptance:

- A small counter fixture renders the expected ports, sequential cells, logic,
  and connections.
- A multi-module fixture supports hierarchy expansion and source cross-probing.
- A large-core fixture opens at the top hierarchy without transferring or
  laying out the entire flattened design.
- Selection survives tab changes; project/job changes invalidate incompatible
  selections explicitly.
- Keyboard navigation and reduced-motion behavior are covered.

## Phase G3 — Critical-path overlay and cross-probing

Normalize timing paths from the exact STA run into an immutable timing-path
artifact tied to the same netlist and constraints. Map path startpoint,
endpoint, ordered cells/pins/nets, incremental delay, cumulative delay, slack,
corner, mode, and clock to gate-graph ids. Preserve unresolved mappings and
report them honestly.

In the viewer, choosing a timing path opens the smallest useful hierarchy/cone,
highlights the ordered path, dims unrelated logic, and shows a compact delay
breakdown. Cross-probing must work in both directions between timing rows,
schematic nodes, source locations, and the selected job evidence.

Acceptance:

- The highlighted path is rejected when its netlist or constraint digest does
  not match the selected graph.
- A fixture proves ordered cell/net mapping and delay totals.
- Partial mapping is labeled partial and identifies every unresolved segment.
- Setup and hold paths, corners, and clocks cannot be confused in the UI.

## Phase G4 — Physical-layout linkage and Part 6 closure

Add the post-P&R companion view only for hash-verified physical artifacts. Use
an indexed, browser-safe representation derived from DEF/ODB for interactive
geometry; keep GDS as downloadable/viewable evidence rather than parsing an
unbounded binary layout directly in the main thread.

Show die/core outline, rows, macros, placed cells at appropriate zoom levels,
routing layers, selected nets, congestion or violations when explicitly
reported, and the gate/path selection mapped into physical coordinates. Move
between schematic and physical views without losing the selected instance,
net, or timing path.

Part 6 is closed only when all of the following are true:

1. A real synthesized netlist can be opened as a bounded hierarchical gate
   schematic.
2. Module/source navigation and schematic cross-probing work in both
   directions.
3. A digest-matched STA path can be highlighted with its measured delay data.
4. A digest-matched post-P&R artifact can locate the same cells/nets in the
   physical view.
5. Missing, stale, partial, truncated, and incompatible evidence is explicit.
6. Reload and backend restart preserve the selected artifacts and durable
   identities.
7. Backend contract tests, frontend lint/build, browser tests, and representative
   small/multi-module/large-design performance tests pass.

## Execution order and update discipline

Implement in this order: D1, D2, G1, G2, G3, G4. D1 and D2 may ship together
after their browser checks pass. G2 must not begin against an ad-hoc frontend
graph: G1's canonical artifact and bounded API are its entry gate. G4 does not
block the first gate-level schematic release, but it is required to declare
Part 6 fully closed.

After each phase, append a completion entry here containing:

- files and schemas changed;
- screenshots or fixture identities used for review;
- exact checks run and their results;
- performance/node-limit measurements where relevant;
- remaining exit criteria and any honest limitation.

No phase is marked complete merely because its empty or unavailable state is
implemented.

## Completion log

### Phase D1 — complete (2026-10-09)

- Files changed: `frontend/src/components/ProjectOverview.jsx` (eleven tiles
  replaced by content-sized chips: status dot + short label only, no
  `flex:1`/fixed width; keyboard-focusable popover carrying evidence, exact
  state, and next action; closes on Escape, outside pointer-down, or gate
  switch; failure glyphs so states stay distinguishable without color;
  `state:` text kept screen-reader visible so the pre-existing gate-state
  assertions still pass), new browser coverage in
  `frontend/tests/browser/d1-qualification-strip.spec.js` (8 tests).
- Behavior verified: at 1600px the identity, all 11 chips, and the
  `next:` indicator share one band with zero strip overflow; at 900px the
  strip scrolls on a single line (height < 30px); at 390px identity keeps the
  first line and the chips take the second.
- Checks run: full browser suite 24 passed at ship time (later 50 with the G2–G4
  suites); `oxlint` 0 errors; `vite build` passed.
- Honest note: the chips moved the state word into the accessible label and
  an `sr-only` span; the pre-existing `toContainText` assertions pass because
  of that span; the visible chip shows only the short label.

### Phase D2 — complete (2026-10-09)

- Files changed: `frontend/src/components/LiveStateDashboard.jsx`
  (`MetricCard` now 46px fixed height, tighter padding/label spacing, value
  single-line with `truncate`; assurance word moved into the card tooltip and
  a compact marker dot), `frontend/src/index.css` (`.metric-card` height 46px
  instead of `min-height: 64px`), new coverage in
  `frontend/tests/browser/d2-metric-band.spec.js` (3 tests).
- Browser snapshots covered: empty state (all eight unavailable with honest
  tooltips), populated, negative timing values (-12.345/-0.004), unusually
  long values at 760px (two rows of four) and 340px (one column); every card
  stayed exactly 46px; Fmax and critical delay verified in the second row.
- Checks run: browser suite green; the pre-existing container-query layout
  test (`4x2/2x4/1x8`) passes unchanged.

### Phase G1 — complete (2026-10-09)

- New module `backend/gate_netlist.py` (`gate-netlist/v1` artifact: schema +
  artifact SHA-256, project/source/constraint/job/netlist digests, module
  hierarchy, cell type/stable id/ports/pin directions/parameters/source
  attributes, nets with driver/load pin refs, tool + library identity,
  explicit completeness/truncation/unsupported-primitive/black-box fields;
  bounded `gate-graph/v1` views with module scope, depth, node limit
  (1..2000), and scope-local cone roots).
- `backend/sc_job.py`: a successful synthesis now emits a structured Yosys
  JSON netlist (best-effort, liberty-loaded for pin directions) under the
  job build dir; failures are reported honestly and never fail the synthesis.
- `backend/eda_tools.py`: `_persist_gate_netlist` canonicalizes, validates,
  and persists the artifact through the evidence store; `backend/jobs.py`:
  durable `netlist_sha256` column (startup migration) so a restarted backend
  keeps the identity; `backend/main.py`: `GET /api/jobs/{id}/netlist` and
  `GET /api/jobs/{id}/netlist/graph?module&depth&limit&cone&cone_hops`
  with `artifact_sha256` pinning (409 on mismatch, 422 on abusive requests).
- Fixture identities: `backend/tests/fixtures/counter8.yosys.json`
  (deterministic, byte-stable; digest
  `ef983cef…`-style canonical output verified twice per test run).
- Checks run: `tests/test_gate_netlist.py` 16 passed (byte stability, digest
  verification, tamper/schema/duplicate/bad-pin rejection, traversal and
  oversized request rejection, restart durability with a fresh
  JobManager + EvidenceRepository, unavailable job honesty, black-box and
  liberty-blackbox leaf rules); the live end-to-end run below.
- Live E2E (real SC screen flow on this host — slang/yosys/OpenSTA under
  `.tools/eda/bin` + `~/.nix-profile/bin`): `g1_probe` synthesized, artifact
  persisted, bounded graph served with 28 nodes / 24 leaf cells / 30 nets,
  all 428 liberty modules reported as black boxes, 0 false hierarchy.
- Honest limitation fixed during the run: Yosys emits integer attributes as
  ASCII bit-vectors, so the first black-box check missed real `blackbox`
  flags and misreported standard cells as hierarchy; fixed and regression
  tested (`test_liberty_blackbox_cell_types_are_leaves_not_hierarchy`).
- Environment note: this agent shell carries a polluted `LD_LIBRARY_PATH`
  (editor extension); the toolchain needs
  `LD_LIBRARY_PATH=<repo>/.tools/eda/lib` and the local EDA binaries on
  PATH for the real flows.

### Phase G2 — complete (2026-10-09)

- New: `frontend/src/components/GateSchematic.jsx` (bounded schematic bound
  to the selected job's artifact: identity strip, module scope breadcrumb +
  scope selector, expandable hierarchy, cone restriction, truncation banner
  with raise-limit action, black-box/unknown-direction disclosure, empty /
  stale / error / loading / unavailable states, search over instances/cells/
  signals/sources, node+net selection with provenance panel, pan/zoom/fit/
  reset, minimap, arrow-key selection navigation, reduced-motion respected),
  `frontend/src/workers/graphLayout.worker.js` (deterministic layered layout
  off the main thread), REST helpers `fetchJobNetlist` /
  `fetchJobNetlistGraph` in `useAgentSocket.js`.
- `LiveStateDashboard.jsx`: the Visualize surface is the schematic; the
  component stays mounted across tab switches so scope and selection
  survive; `VerilogView.jsx` gained `focusSource` so source attributes
  cross-probe into the Sources surface and back.
- Browser coverage: `g2-gate-schematic.spec.js` (5 tests) + shared endpoint
  mock `g2-gate-schematic-mock.js`: counter fixture renders expected ports,
  a sequential cell (dfxtp with clock glyph), logic and wires; hierarchy
  expansion into `alu_slice` and back; source cross-probing to Sources and
  back with the same selection; truncated view + raise-limit; keyboard
  navigation under `prefers-reduced-motion: reduce`.
- Checks run: 50/50 browser suite at the time of the G4 record, `oxlint` 0
  errors, build passed.
- Bugs found and fixed during browser verification: the pan handler's
  pointer capture swallowed node clicks (pan now only starts on the
  background); a fit computed against the hidden (zero-size) canvas was
  never repeated when the tab became visible (refit on visible-size
  transition); the worker's layer fallback tested `has(node)` instead of
  `has(node.id)` and zeroed every layer, collapsing the schematic into one
  column.
- Honest limitation: the schematic currently opens one module scope per
  request; depth > 1 inlines one extra hierarchy level per request under
  the same node limit (the browser never receives an unbounded flattened
  design), but large-core performance was only exercised through the
  node-limit/truncation paths; a multi-hundred-thousand-cell netlist remains
  untested.

### Phase G3 — complete (2026-10-09)

- New module `backend/timing_paths.py` (`timing-paths/v1` artifact: ordered
  segments with kind (clock/cell_pin/net/port), raw name, measured delay and
  cumulative time, arrival/required/slack/MET-VIOLATED, corner, clock, path
  group, analysis; per-path `mapping` with resolved/unresolved split and an
  honest `partial` flag; identity bound to the exact netlist artifact +
  constraint clock period; digest-verified on load).
- Parsing proven against the real OpenSTA `setup.topN.rpt` / hold /
  scenario-corner tables SiliconCompiler writes; fixture
  `backend/tests/fixtures/timing.setup.topN.rpt` (two setup paths through
  the counter8 netlist) — ordered cell/net mapping and delay totals are
  asserted against the same fixture the graph endpoint serves.
- `backend/eda_tools.py`: `_persist_timing_paths` persists the artifact via
  the evidence store; `jobs.py`: durable `timing_paths_sha256` column;
  `main.py`: `GET /api/jobs/{id}/timing-paths?netlist_sha256=` (409 on
  mismatch). `GateSchematic.jsx`: timing-path strip (setup/hold, corner,
  clock always visible and color-separated), cone auto-open on the path's
  first mapped cell, amber path highlight + dimmed unrelated logic, compact
  delay breakdown with click-through cross-probing to schematic nodes and
  nets, explicit digest-mismatch rejection banner, honest unavailable strip.
- Checks run: `tests/test_timing_paths.py` 10 passed (ordered parsing with
  arrival/required/slack totals, hold vs setup distinction, digest binding +
  tamper/schema rejection, resolved graph-id mapping, explicit partial
  mapping, build-dir collection with scenario corners, REST 409/404,
  restart durability, unavailable honesty);
  `g3-timing-paths.spec.js` 4 passed (highlight+cone+dim, partial labeling,
  digest-mismatch rejection, unavailable state); full suites green.
- Live E2E (real screen synthesis of `g1_probe`): the STA hold path
  `q[0]$_DFF_PN0_ -> _21_ -> _24_` mapped 10/10 segments (cells, nets, port)
  with per-pin delays and was served from the durable store; every mapped
  node existed in the bounded graph the endpoint returned.
- Honest limitation: mapping resolves against the top-module scope (SC
  netlists are flat); a hierarchical synthesis would need per-module
  resolution, recorded as partial rather than guessed today.

### Phase G4 — implemented and fixture-checked (2026-10-09)

- New module `backend/physical_layout.py` (`layout-view/v1`: bounded, indexed
  DEF-derived view — die area, rows, placed components with coordinates and
  netlist-mapped `graph_id`s, IO pins with nets, nominal cell footprint from
  the DEF's own row site/pitch and flagged as nominal, truncation and
  partial-mapping completeness; digest-bound to the same netlist/constraint
  identity; GDS remains downloadable evidence and is never parsed here).
- `backend/eda_tools.py`: `_persist_layout_view` (only a run whose flow
  emitted a DEF produces a view); `jobs.py`: durable `layout_view_sha256`
  column; `main.py`: `GET /api/jobs/{id}/layout?netlist_sha256=` (409 on
  mismatch). New `frontend/src/components/PhysicalLayout.jsx`: die/rows/cells/
  pins rendering with zoom-adaptive cell dots vs nominal rectangles, pan/
  zoom/fit/reset, selected-instance location and centering, timing-path cell
  highlighting, honest unavailable state; `LiveStateDashboard` gained the
  schematic/layout sub-view with shared selection (instance selected in one
  view is located in the other, both directions).
- Fixture identities: `backend/tests/fixtures/counter8.def` (same component
  names as the counter8 netlist fixture, one unplaced macro, two rows).
- Checks run: `tests/test_physical_layout.py` 7 passed (DEF parsing incl.
  nominal footprint from row pitch, digest binding, mapped graph ids,
  partial mapping honesty, build-dir collection, REST 409/404, restart
  durability, screening-unavailable); `g4-physical-layout.spec.js` 3 passed
  (die/cells/pins render + selection location, bidirectional probing,
  unavailable state). At that historical G4 checkpoint the backend suite had
  509 passes and the browser suite had 36 passes; lint and build were clean.
- Live P&R validation (2026-10-09, after the owner approved installing
  klayout into the user nix profile — same precedent as OpenROAD; see
  `scripts/install-klayout.sh`): a real `flow_mode="pnr"` run of `g4_probe`
  (full ASIC flow, stage place_route, klayout 0.30.12) produced the gate
  netlist AND the layout view from the actual routed `g4_probe.def.gz`:
  die 22925x22925 DBU, 7 rows, 12 IO pins, 72/72 placed components mapped
  onto gate-graph ids with zero missing from the served graph; the flop
  `q[0]$_DFF_PN0_` located at physical (4600, 16320). Two parser fixes came
  out of the live run and are covered by the fixture tests' formats:
  SC stores routed DEFs gzipped and DEF-escapes identifiers
  (`q\[0\]$_DFF_PN0_`), and OpenROUTE writes `+ DIRECTION` pins with
  placement inside `+ PORT` blocks.
- A second live-run fix landed in `sc_job.py`: post-P&R netlists connect
  physical-only pins (VPWR/VGND) that the liberty view does not define, so
  the structured emission now runs `hierarchy -top` without `-check`
  (unknown-pin connections are ignored instead of aborting) and finds
  liberty files both through the manifest and directly under
  `job0/*/0/inputs/*.lib`.
- Remaining exit criterion (honest): representative large-design
  performance (a multi-hundred-thousand-cell netlist) is still only
  exercised through the bounded node-limit/truncation paths.

### Part 6 closure status (2026-10-09)

1. Real synthesized netlist opened as a bounded hierarchical schematic —
   live-validated (G1+G2 E2E, real SC screen flow).
2. Module/source navigation and schematic cross-probing both directions —
   browser-verified.
3. Digest-matched STA path highlighted with measured delay data —
   live-validated (G3 E2E) and fixture/browser-verified.
4. Digest-matched post-P&R artifact locating the same cells in the physical
   view — live-validated (real pnr run; 72/72 components mapped, zero
   missing from the gate graph).
5. Missing/stale/partial/truncated/incompatible evidence explicit —
   tested at every layer (backend units + REST + browser states).
6. Reload/backend-restart preserve artifacts and identities — restart
   durability tested for all three artifacts (fresh manager + repository on
   the same durable roots).
7. Backend contract tests 509 passed; frontend lint 0 errors; frontend build
   passed; later C5 coverage added deterministic 100,000-cell backend and
   150,000-cell browser fixtures. The browser run keeps 500/1,000-node
   responses bounded and cleans up heap state, but the isolated performance
   report measured 141 ms zoom and a 181 ms maximum long task. Earlier runs
   recorded 132–133 ms and 174–176 ms; a parallel rerun reached 328 ms for
   zoom. All values exceed the 100 ms budget. Large-design interaction
   performance remains the one open Part 6 criterion.
