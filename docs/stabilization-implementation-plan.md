# Manufacturable multi-file design roadmap  -  implementation handoff

Status: Phases 1-2 COMPLETE; Phase 3 BOUNDED P&R ATTEMPT COMPLETE but physical closure remains open; Phases 4-5 NOT STARTED. The completed S1-S8 stabilization plan has been replaced. Its evidence remains in `docs/s8-matmul-qualification-audit.md`, `docs/roadmap.md`, tests, and run artifacts.
Created: 2026-10-05.

## Purpose and execution rules

Move from a single generated RTL blob and demonstration-PDK screening toward auditable multi-file hardware projects that can qualify an established core, close physical implementation gaps, enter the Tiny Tapeout flow, and only then generate a small RV32I implementation.

Execute phases in order unless a documented dependency changes. Preserve existing projects and the legacy single-file interface during migration. Bind every source set, constraint set, build configuration, verification input, tool identity, and result with immutable hashes. Passing simulation or an open-source demo-PDK flow yields engineering evidence; commercial foundry signoff is a separate assurance level.

Do not use paid model calls for deterministic implementation or qualification work. Preserve the current UI direction. Each handoff records changed files, checks run, unavailable checks, durable evidence, and unmet exit criteria.

## Phase 1  -  Multi-file project foundation

Implementation status: COMPLETE (2026-10-05).

1. Introduce a versioned `hardware-project/v1` manifest with normalized source paths, roles/languages, content hashes, compile order, include directories, defines, top module, testbenches, constraints, and named build targets.
2. Store file bodies as immutable content-addressed artifacts. The manifest digest identifies the exact source set. Reject unsafe paths, duplicates, missing artifacts, hash mismatches, invalid tops, and ambiguous compile order.
3. Preserve legacy projects through a lossless single-file projection. Existing `verilog_code` tools and Code view continue working during migration; artifact handles never become displayed or executable source.
4. Add project-scoped APIs for reading and atomically replacing a manifest/file set. Failed validation must not replace the previous active manifest.
5. Replay the active manifest in `project_state`. Add a compact Sources view with file selection and hierarchy-ready metadata; do not represent regex guesses as authoritative hierarchy.
6. Bind later run manifests to the hardware-project digest, constraint digest, selected target, tool versions, and top. Results from another source set must never populate the active dashboard.

Exit: a multi-file fixture survives save/reload/backend restart with identical manifest and file hashes; unsafe or incomplete updates fail atomically; a legacy project loads unchanged; the UI selects every source without concatenation.

## Phase 2  -  Qualify the platform with a known small RV32I core

Implementation status: COMPLETE — BOUNDED QUALIFICATION BASELINE (updated 2026-10-09). Manufacturing qualification and signoff are not claimed; see `docs/phase2-known-core-qualification.md` and `docs/phase2-known-core-physical-evidence.md`.

1. Use two pinned, unchanged reference cores with distinct roles: PicoRV32 is the open-tool control/calibration core; minimal Ibex is the actual multi-file SystemVerilog qualification target. The educational EkthaReddy and EcrioniX projects serve as decomposition/UI inspiration only; correctness comes from the pinned reference cores.
2. Import it unchanged, preserve license/provenance, pin the commit, and capture native source order, parameters, memory assumptions, constraints, tests, and expected results.
3. Run production lint, elaboration, module/integration simulation, independent ISA compliance, synthesis, STA, and one bounded P&R qualification.
4. Present source hierarchy, diagnostics, runs, and evidence without flattening the design. Separate platform, environment, and DUT failures.

Exit: the unchanged core has a reproducible report bound to one project digest, or a bounded report identifying the platform gap without modifying the reference core.

Qualification order and gates:

- PicoRV32 control: native source, license and commit provenance; lint/elaboration; existing tests and RVFI/formal where available; Sky130 synthesis/STA and at most one bounded P&R run.
- Minimal Ibex target: native FuseSoC source order/configuration; Verilator lint/elaboration and upstream verification inputs; explicit `sv2v` preprocessing provenance for Yosys/OpenROAD; independent ISA checks; the same bounded Sky130 evidence gate.
- A PicoRV32 failure is a fundamental execution-flow failure. PicoRV32 passing while Ibex fails identifies a multi-file/SystemVerilog/package/preprocessing platform gap. Phase 2 is qualified only by Ibex; PicoRV32 alone cannot close it.
- Never patch either reference core to make the platform pass. Adapters, wrappers, constraints, preprocessing and environment limitations are recorded separately and bound to immutable hashes.

### Phase 2 physical-evidence closure work

Implementation status: COMPLETE WITH RECORDED PHYSICAL GAPS (updated 2026-10-09).
The exact-constraint Ibex baseline and bounded configuration/strategy candidates
remain below the unchanged 50 MHz product goal; the best preserved Ibex screen
is 30.54 MHz. The corrected pre-CHERIoT target at `e4bcf749...` eliminated
unconstrained paths but retained timing, electrical, and post-synthesis
equivalence failures. The second-core PicoRV32 control was then re-screened with
an explicit port-complete 20 ns contract: setup closes at 54.03 MHz (+1.49 ns)
with zero unconstrained endpoints, proving the platform can cross 50 MHz on a
known RV32I control. It is not physically qualified: hold is -0.17 ns, 2,519
slew / 4 fanout / 40 capacitance violations remain, and mapped-netlist
equivalence leaves 145 cells unproven. P&R was therefore not admitted and no
constraint was loosened. Exact evidence and remaining gates are recorded in
`docs/phase2-known-core-physical-evidence.md`.

A bounded equivalence-method diagnostic confirmed that enabling undefined-state
modeling does not reduce the 145 cells. A reset-aware miter exposes independent
initial values in intentionally unreset PicoRV32 state when RTL and mapped
netlist are loaded separately. This is now classified as inconclusive rather
than a counterexample. An in-process `equiv_opt` experiment then preserved the
pre/post transformation state relationship. The proof runner is now fail-closed,
digest-binds the exact transformation, and correctly honors a terminal
zero-unproven proof status. PicoRV32 generic synthesis still leaves 3,028 points
unproven with undefined-state modeling and 431 without it. These results remain
inconclusive and involve no functional counterexample. Phase 2 remains blocked
on an explicit reset/state-correspondence proof before exact Sky130 mapping and
any P&R admission.

Legacy evidence filenames and schema identifiers beginning with `phase3-` are
retained as immutable historical record identifiers. They do not indicate that
roadmap Phase 3 has started.

Phase ownership of current screening limitations:

- The old open `skywater130_demo` cells and its slow corner remain a conservative
  Phase 2 comparison baseline. Phase 3 will replace them only when a reviewed,
  legally available target library/corner exists; Phase 4 runs the exact pinned
  Tiny Tapeout provider stack. Neither phase may rename demo evidence signoff.
- Synthesis-only timing and the AREA3 area-oriented strategy are resolved by
  Phase 2 qualification review plus bounded timing-oriented synthesis/P&R and
  equivalence evidence. Phase 4 repeats physical hardening in the official
  provider flow.
- The upstream-default Ibex artifact is historical evidence. The current
  digest-bound profile applies `RV32M=None`, `RV32B=None`, and minimum-supported
  `RV32Zca` before conversion. Native-to-converted equivalence is now proven
  for all 15,730 comparison cells using independent native and converted
  frontends; independent ISA/formal checks remain before P&R.

1. Replace fallback SDC assumptions with explicit reviewed clocks, uncertainty, I/O delays, drive cells, loads, resets, exceptions, and operating corners.
2. Diagnose slew, capacitance, and fanout violations from exact routed reports. Fix root causes or record narrow waivers; never hide them by globally relaxing limits.
3. Resolve floating nets or classify each intentional case with netlist evidence.
4. Add post-synthesis equivalence and then post-layout/netlist equivalence where supported. Add SDF gate-level simulation as complementary evidence alongside the other checks.
5. Archive constraints, reports, netlists, tool identities, and hashes.

Exit: explicit constraints exist, no electrical/floating-net violation is unexplained, and equivalence outcomes are recorded. Unsupported checks remain unavailable and are recorded as such.

## Phase 3  -  Close physical-evidence gaps on the Phase 2 qualified core

Implementation status: BOUNDED P&R COMPLETED — PHYSICAL ACCEPTANCE FAILS ON TIMING AND SLEW (updated 2026-10-10). See `docs/phase3-physical-gap-audit.md`.
Phase 2 provides the unchanged Ibex baseline, accepted native-to-converted
equivalence, independent ISA/lint/formal evidence, and one completed bounded P&R
screen. This admits Phase 3 without claiming physical qualification or signoff.

The pre-P&R portion of Phase 3 is complete: the exact 61-port interface,
reviewed port-specific SDC, deterministic physical-report policy, provenance
checks, and durable baseline gate evidence exist. The historical routed baseline
correctly fails admission (setup, slew, capacitance, and constraint mismatch).
The first bounded P&R run failed closed at global routing with 46 total overflow.
A separately authorized density-controlled run preserved the RTL, reviewed 20 ns
SDC and synthesis strategy; it completed detailed routing and emitted GDS, SPEF,
SDF and final netlists. Its physical gate fails on −2.33 ns setup, −0.03 ns hold
and 791 max-slew violations. OpenROAD detailed-route DRC and antenna are zero;
floating, overdriven and functional unconstrained endpoints are zero. Post-layout
equivalence is inconclusive with 25,700 cells unproven. SDF simulation,
independent KLayout DRC/LVS, 50 MHz closure, manufacturability, provider
acceptance and signoff remain open.

## Phase 4  -  Tiny Tapeout wrapper and official hardening

Implementation status: NOT STARTED.

1. Wrap the qualified core in the exact supported Tiny Tapeout interface and freeze clock, reset, I/O, memory, and software-loading behavior.
2. Add provider-specific pin, area, power-domain, and configuration requirements. Verify wrapper/core boundaries independently.
3. Run the official pinned Tiny Tapeout hardening and precheck flow. Preserve GDS, LEF, netlists, reports, constraints, logs, containers, and hashes.
4. Distinguish provider acceptance, open-source hardening evidence, and foundry signoff.

Exit: a reproducible official hardening/precheck result exists for the exact wrapped project, or a bounded blocker report names the failed provider requirement.

## Phase 5  -  Generate and qualify our own small RV32I

Implementation status: NOT STARTED.

1. Freeze the ISA subset, privilege assumptions, exceptions, memory interface, reset, latency/throughput, debug/software loading, and implementation goals.
2. Generate modules incrementally using bounded structured file patches rather than one monolithic response.
3. Require module tests, independent ISA comparison, formal properties, regressions, and compliance before physical optimization.
4. Compare against the known-core baseline. Preserve best verified revisions and never repair timing by silently changing behavior.
5. Apply the Phase 3 evidence and Phase 4 wrapper/hardening gates.

Exit: the generated core satisfies its frozen contract and the same gates as the known core, or reports a bounded reproducible gap.

## Remaining work retained after this roadmap

- Project export/import using a versioned portable archive with the project record, content-addressed RTL/testbench artifacts, contracts, run manifests, evidence/job references, hashes, and compatibility metadata. Import validates every hash and never substitutes missing artifacts.
- Broader golden models and generated-design regressions; functional coverage; formal properties/equivalence; CDC/RDC and production lint gates. Verify applicability and evidence rather than treating installed tools as qualification.
- General autonomous-design qualification beyond matmul and RV32I, with design-specific contracts and budgets.
- Gate-level visualization from real synthesized gates/connections, hierarchy, and critical-path evidence linked to the exact netlist/report.
- Persisted run-status UI hydration across backend/container restarts: consume the backend `project_state.run_state`, preserve interrupted/terminal state and the last stage, and regression-test the status popup after reconnect.
- Engineer CLI/TUI over the same APIs, followed by beginner/intermediate/expert web presentations and additional restricted GenUI actions.
- Optional FPGA-board flow and hardware-in-the-loop after a board is selected. This remains separate from the primary Sky130/Tiny Tapeout objective.
- Manufacturing submission review beyond wrapper hardening. Demo-PDK screening and estimated power are not provider/foundry signoff.
- Later manufacturing order/payment/tracking integration after provider selection and explicit authorization boundaries.
- Authentication, ownership, origin controls, and deployment resource limits before any network deployment. Local optimization/token budgets remain in scope.
