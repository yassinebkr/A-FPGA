# Manufacturable multi-file design roadmap  -  implementation handoff

Status: Phases 1-2 COMPLETE; Phase 3 CLOSED AS A BOUNDED SKY130 DEMO-PDK MILESTONE WITH PHYSICAL ACCEPTANCE FAILING; Phases 4-5 NOT STARTED. The completed S1-S8 stabilization plan has been replaced. Its evidence remains in `docs/s8-matmul-qualification-audit.md`, `docs/roadmap.md`, tests, and run artifacts.
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

Implementation status: CLOSED AS A BOUNDED SKY130 DEMO-PDK MILESTONE — PHYSICAL ACCEPTANCE FAILS ON TIMING AND SLEW (updated 2026-10-10). See `docs/phase3-physical-gap-audit.md`.
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

A third bounded run enabled the previously skipped OpenROAD post-route timing
repair task while preserving every other reviewed input. It improved the final
screen to 45.00 MHz / -2.22 ns setup and reduced max-slew violations from 791
to 106. Hold remains -0.03 ns; DRC, antenna, capacitance, floating,
overdriven, and functional unconstrained counts remain zero. The candidate
fails closed and is retained as diagnostic evidence. The next timing action is
an exact critical-path architectural/configuration review; further tuning of
the same physical controls is outside the bounded experiment.

### Sky130 milestone closure decision

The Sky130 exploration is frozen at the v3 repair evidence. It established a
reproducible RTL-to-GDS engineering path, reviewed constraints, durable physical
reports, clean detailed-route DRC and antenna counts, and fail-closed acceptance
gates. It also located the present Ibex limit: 45.00 MHz with -2.22 ns setup,
-0.03 ns hold, and 106 max-slew violations. These measurements remain screening
evidence from the open demo library and slow corner.

Sky130 remains available as a deterministic regression backend and for the
separate Tiny Tapeout provider flow. Routine work must not launch another Ibex
Sky130 optimization run unless a written hypothesis changes the architecture,
core configuration, library/corner, constraint model, or physical methodology.
The next roadmap decision is the architecture and implementation-target matrix
for the larger SoC direction. Frequency goals will be stated per target and per
evidence level.

## Phase 4  -  Define and qualify the open microcontroller

Implementation status: NOT STARTED.

1. Freeze the product contract: license, RV32 ISA profile, privilege and exception behavior, debug, interrupts, pipeline, memory map, boot, bus, peripherals, clocks, resets, power intent, IO and modification boundaries.
2. Select the implementation profiles. IHP SG13G2 is the leading open physical candidate and must pass a versioned PDK/library/MPW-readiness review. A separate simulation profile remains technology-independent.
3. Build the MCU incrementally as a multi-file hardware project with independently reviewed reference models, ISA compliance, firmware tests, assertions, functional coverage, formal properties, CDC/RDC and production lint.
4. Integrate ROM, SRAM interfaces, interconnect, timer, interrupt controller, GPIO, UART, SPI and debug in bounded increments. Freeze a reproducible boot and firmware demonstration.
5. Establish 250 MHz as the primary implementation objective and 500 MHz as the stretch objective. Preserve measured baselines and require explicit architectural hypotheses for timing revisions.

Exit: the complete MCU and firmware satisfy the frozen functional contract, all applicable verification gates, and reproducible pre-layout timing evidence for the selected target.

## Phase 5  -  Manufacture, bring up and revise the open microcontroller

Implementation status: NOT STARTED.

1. Run placement, CTS, routing, extraction, multi-corner STA, electrical checks, DRC, LVS, antenna, equivalence and gate-level checks using the exact provider-accepted stack.
2. Freeze GDS, netlists, constraints, reports, firmware, test vectors, tool/container identities, licenses and hashes as one release candidate.
3. Complete provider review, MPW submission, packaging choice, test-board design and a documented manufacturing handoff.
4. Bring up returned silicon with clock, reset, debug, memory, peripheral, power and frequency characterization. Compare measurements with pre-silicon evidence.
5. Record defects and modifications as versioned engineering changes, rerun affected gates and manufacture a revised candidate when justified.

Exit: a packaged device executes the frozen firmware demonstration, carries traceable manufacturing evidence, and has a reproducible modification/revision procedure. The release report records the achieved frequency; 250 MHz is the product target and 500 MHz remains a stretch target until measured on silicon.

## Future milestone  -  Linux-capable RV64 SoC around 1 GHz

Retain a Linux-capable open RISC-V SoC around 1 GHz as a later product milestone. CVA6 and Cheshire are current reference candidates. This milestone requires an application-class core, MMU, caches, high-performance memory subsystem, scalable interconnect, boot/software platform and a more advanced implementation technology. It is intentionally outside the active MCU execution path.

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
