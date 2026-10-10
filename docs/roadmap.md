# Engineering roadmap and status

This is the current execution order. Phases 1--3 are implemented as engineering gates; Phase 4 is the remaining platform milestone. The backlog after Phase 4 records work that remains afterward; it must not be mistaken for completed capability.

## Active roadmap

### Strategic milestone — open microcontroller from specification to silicon

The active platform milestone is an entirely open, modifiable microcontroller carried from specification through RTL, verification, firmware, physical implementation, manufacturing package, packaged-device bring-up, and documented silicon revisions. It is a demanding end-to-end qualification vehicle for this FPGA/ASIC design platform. The platform remains the primary project in this repository. Once the milestone is mature enough, the microcontroller will be derived into a separate product repository with its own identity, releases, governance, and silicon lifecycle.

The milestone's primary clock objective is 250 MHz. A 500 MHz result is an explicit stretch objective whose feasibility must be established for the selected process, libraries, memories, interfaces, corners, voltage, and physical design. Frequency claims require post-route evidence and are qualified per technology.

The initial architecture is expected to be a compact 32-bit RISC-V microcontroller-class SoC. The completed Croc reference audit is recorded in [docs/croc-reference-audit.md](croc-reference-audit.md). ISA profile, privilege model, pipeline, interrupt/debug architecture, memory map, SRAM/ROM strategy, bus, peripherals, boot path, IO, package, process, license, and software contract remain decisions to freeze before RTL implementation. IHP SG13G2 is the leading open physical-target candidate; its early-access status and MPW acceptance requirements must be qualified before selection.

A Linux-capable 64-bit RISC-V SoC targeting approximately 1 GHz is retained as a future milestone. CVA6/Cheshire remain useful reference systems for that later stage and for large-project platform qualification. Work on that milestone resumes after the microcontroller has completed a silicon bring-up and revision loop.

### Phase 1 — Architecture-first generation (implemented)

Implemented and verified. A strict versioned `ArchitectureContract` captures the target technology (FPGA or ASIC), clock target in MHz and ns, latency and throughput, pipeline stages, multiplier/DSP strategy, memory/register strategy, and top module. The contract is canonically serialized, persisted with a SHA-256 digest, and revalidated after project reload. `define_architecture` is the first tool for design chats. The controller gates lint, simulation, and synthesis against the approved contract; changing the contract digest invalidates prior RTL checkpoints. The focused Phase 1 suite passes 25 tests.

Exit criterion: every generated design has an explicit, validated microarchitecture before RTL is written.

### Phase 2 — Deterministic timing optimization (implemented)

Implemented and verified. The controller now turns measured synthesis/STA evidence into bounded, explainable timing retries:

- `timing_optimizer.py` selects the measured worst setup path and preserves its exact startpoint, endpoint, slack, evidence text, and RTL SHA-256 binding.
- Paths are classified into `multiplier_to_accumulator`, `wide_adder_carry_chain`, `mux_decode`, `fanout`, `memory_read`, `control`, or `unknown`. Unknown or low-confidence paths fail closed and authorize no speculative strategy.
- Each category has an explicit strategy allow-list: for example product/accumulator pipelining, balanced adder trees, mux-depth reduction, fanout reduction, memory-read registration/BRAM inference, or control pipelining. A retry must target the measured endpoints and use a strategy supported by the category.
- The approved architecture contract remains authoritative: its clock period/target Fmax is required for synthesis, and latency/throughput changes are rejected. A measured Fmax below the contract target is a timing failure even when a differently constrained run reports positive slack.
- Timing evidence is bound to the candidate RTL. Stale diagnoses and retry constraints are cleared when the measured candidate changes; repeating the same strategy in one run is blocked before EDA starts.
- Structured progress/decision events expose timing failures, target-Fmax misses, classification, recommended strategies, metric deltas, rejection/restoration, and screen/full-P&R budgets without exposing model reasoning.

The focused Phase 2 integration suite passes 31 tests. The S8 evidence audit recovered the exact r7 source and drove bounded critical-path optimization to candidate `2dc1e3e6…`. Three independent 40-vector functional/latency trials pass, and a 20 ns Sky130 screen using the explicit Yosys `DELAY1` strategy measured +0.34 ns / 50.86 MHz with zero unconstrained paths. One later LibreLane 3.0.14 full P&R run completed at 20 ns with worst multi-corner setup/hold slack of +2.389/+0.103 ns and clean routing/Magic/KLayout DRC, LVS, XOR and antenna gates. It used generic fallback SDC constraints and retains electrical-rule warnings, two floating nets and no EQY result, so it is routed engineering evidence rather than signoff.

Exit criterion: the controller explains the measured critical path, selects only an evidence-backed transformation, preserves the architecture and functional contracts, and rejects regressions without relying entirely on model intuition.

### Phase 3 — Matmul qualification gate (implemented; qualification run pending)

The repository now freezes `systolic_mm_4x4` as a 4×4 signed-INT8 benchmark with exact signed-INT32 accumulation, an indexed load/start/busy/done/indexed-read interface, ten-cycle latency and initiation interval, and a 50 MHz/20 ns Sky130 target. A human-authored Python reference model carries version and source hashes. The corpus contains signed extremes, zero/identity/mixed cases, per-output basis isolation, and three deterministic random streams.

`scripts/run-matmul-qualification.py` provides free preflight/dry-run modes and an actual RTL path. Given `--rtl`, it compiles a generated protocol checker with Icarus, checks every result against Python-produced immutable expectations, enforces latency and handshake behavior, repeats trials when requested, and writes append-only spec/corpus/trial/aggregate manifests under `backend/data/qualifications`. Sky130 synthesis+STA runs only with explicit `--timing`; no LLM is invoked. Exit codes distinguish pass (0), qualification failure (1), infrastructure-inconclusive (2), invalid input (3), and execution/cancellation (4).

The benchmark core interface is intentionally an internal qualification contract. The Tiny Tapeout user-project wrapper and its provider IO integration belong to the later hardening/submission work. Candidate `2dc1e3e6…` has passed the independent functional/latency gate, explicit 50 MHz screening gate and one standalone full-P&R engineering run. Tiny Tapeout wrapper integration and provider hardening remain separate required evidence. See [the S8 audit](s8-matmul-qualification-audit.md).

Exit criterion: repeated clean candidate evaluations meet the functional/protocol and 50 MHz criteria, or persist a precise reproducible failure/inconclusive manifest. Qualifying autonomous generation across fresh model sessions is still evidence to collect.

### Phase 4 — Real FPGA target flow (optional later track)

Select one supported development board and integrate the corresponding open-source FPGA flow, initially Yosys plus nextpnr where the device family is supported. Add board and pin constraints, clock definitions, programming, and device-specific reporting for LUTs, flip-flops, BRAMs, DSPs, routed Fmax, and utilization. This phase does not block the primary Sky130/Tiny Tapeout ASIC direction and can begin before a board is purchased; hardware-in-the-loop remains deferred until hardware is available.

Exit criterion: one design can be evaluated independently as an ASIC implementation and as a routed implementation for the selected FPGA target.

These phases are engineering qualification work. They do not constitute FPGA-vendor signoff, silicon signoff, or commercial/foundry signoff.

## Remaining work after Phase 4

The following items remain after the four-phase optimizer milestone, unless explicitly marked complete elsewhere in the repository:

- Add versioned project export/import so projects can be backed up and moved with their exact RTL/testbench artifacts, contracts, run manifests, evidence references and hashes. Imports must validate compatibility and content hashes before restoring executable state.

- Apply independent golden reference models, functional coverage, formal properties/equivalence, CDC/RDC, and a production-grade lint policy to generated designs as release gates. The reusable verification infrastructure exists, but the generated-design workflow and broader corpus still need qualification.
- Build a concrete FPGA profile, adapter, programmer integration, and hardware-in-the-loop harness once a development board is selected and available. Until then, hardware evidence remains inconclusive.
- Model-output streaming and stall detection are implemented: provider text streams into one assistant response, provider-exposed reasoning stays in a bounded folded Working tile, fragmented tool calls are assembled before execution, and first-token/inactivity/overall timeouts fail safely. Durable cross-run token/cost accounting and clearer retry/revision explanations remain.
- After analysis, add an interactive gate-level representation to each project: render the synthesized netlist as recognizable gates and connections, with critical-path highlighting and evidence links back to the analyzed run.
- The restricted GenUI Design Contract and its execution gate are implemented: a natural-language design request produces a proposal, pauses before RTL/EDA, resolves required decisions, and resumes the saved request only through revision/digest-bound `Approve & build`. Material revisions replace the executable architecture binding and invalidate stale checkpoints. Extend the same static app-owned protocol to expert constraints, critical paths, implementation settings, assurance classes, and evidence artifacts while preserving the current visual direction. Add beginner/intermediate/expert presentation layers later over the same engineering backend. Generic executable widgets and direct EDA actions remain intentionally disabled.
- Implement the engineer-focused terminal interface (CLI/TUI) over the existing REST, WebSocket job/log, verification, and evidence APIs. It should support project selection, job launch/cancel/subscribe, live logs, timing/PPA summaries, artifact hashes, and JSON/text export.
- Complete the provider/cloud-specific Tiny Tapeout submission and review flow. For a shared-wafer or other manufacturing route, later add the selected provider’s process/package constraints, submission checklist, payment and order tracking integration, and release review. Payment is not part of the local execution path today.
- Add authentication, ownership, origin controls, and resource limits before any network deployment. These controls are intentionally deferred while the tool is local and single-user.

## Completed foundations and explicit limits

The repository already contains the evidence schemas, independent reference-model examples, verification orchestration, structured job/log delivery, timing-search tooling, FPGA profile schema, and a local Tiny Tapeout Sky130 hardening record described in the main README and [docs/tapeout.md](tapeout.md). Those foundations do not imply that every generated design has passed every gate.

Current limitations that remain important when interpreting results:

- SiliconCompiler demo PDK runs, estimated power, and coarse Fmax searches serve as comparison evidence; signoff analysis is a separate assurance level.
- The local Tiny Tapeout hardening result is not provider acceptance or foundry signoff; cloud submission and provider review remain pending.
- A shared-wafer price estimate is not a manufacturing commitment. The exact shuttle, PDK, IO/wrapper contract, die limits, rule decks, and provider checks must be selected before purchase.
- The agent is still local-only and single-user. No network deployment should be inferred from the current UI or API.

## Sky130 milestone boundary

The bounded Sky130 demo-PDK exploration is closed as of 2026-10-10. Its frozen
Ibex comparison baseline is the routed v3 repair candidate: 45.00 MHz, -2.22 ns
setup, -0.03 ns hold, 106 max-slew violations, and zero reported detailed-route
DRC, antenna, capacitance, floating-net, overdriven-net, and functional
unconstrained-endpoint violations. Physical acceptance therefore fails.

Sky130 remains a supported regression and Tiny Tapeout implementation target.
New optimization experiments require an explicit change hypothesis and a fresh
evidence budget. The next planning gate selects the scalable core/SoC
architecture and target-technology profiles; each profile will carry its own
frequency, verification, physical-evidence, and signoff expectations.
