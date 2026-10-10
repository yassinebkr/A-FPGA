# Phase 3 routed-Ibex physical-gap audit

Date: 2026-10-10
Status: bounded P&R completed; physical acceptance fails on timing and slew

## Scope

This audit began from the immutable Phase 2 routed Ibex baseline and now also
records one bounded Phase 3 implementation attempt. It does not modify the
reference core or claim signoff. Baseline manifest:
`/mnt/cold-hdd/sc_builds/phase2-ibex-specialized-pnr-filetype-fix/ibex_core/job0/ibex_core.pkg.json`.

## Findings

- The final floating-net report is empty (SHA-256
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`).
  This is clean evidence for that report; the broader physical checks
  supported by a signoff tool have yet to run.
- The final DRV report contains **464 max-slew** and **7 max-capacitance**
  violations (SHA-256
  `09410bc5ce1ebdab762ca41423314108ae57489cb449b4c0d552f9b60d86d7d2`).
- The final high-fanout report lists 50 nets and reaches fanout **126**
  (SHA-256
  `04d04ee8c123d1c092ce807477b88a42069e1cfc6519f60a89d0853c3de96fef`).
  A high fanout is diagnostic evidence that requires correlation with
  transition/capacitance and buffering before it can count as a violation.
- The final timing report contains one nominally unconstrained path from
  `irq_fast_i[11]` to inserted antenna diode `ANTENNA_24/DIODE` (SHA-256
  `48c3965f2f63ec4a48fb0014ab07d72a4ee47e3438d663d023da85292f218750`).
  Therefore the Phase 2 `unconstrained_endpoints: 0` metric is insufficient by
  itself. The path appears to terminate on a physical-only diode, but this must
  be classified by an explicit report policy rather than silently ignored.
- The SDC (SHA-256
  `906865785c0d8cd6862be5d5ac49569fc52d604e2f1581cbfdf1369186e6431b`)
  uses blanket `[all_inputs]` and `[all_outputs]` constraints. It therefore also
  applies generic input delay/drive assumptions to `clk_i` and `rst_ni`. This is
  a useful screening SDC; a reviewed Ibex integration contract is a separate
deliverable.

## Decision

Stop further PicoRV32 induction tuning. PicoRV32 has served its calibration
role. Preserve its inconclusive proof records and the reusable fail-closed
transformation-equivalence runner, but do not make Pico proof closure a Phase 3
entry gate.

The Phase 3 execution order is:

1. **Complete:** define a reviewed Ibex integration environment. The exact
   pinned converted artifact has 61 ports: 27 functional inputs, 32 outputs,
   one clock, and one asynchronous reset. Every functional port has an explicit
   delay plus drive/load disposition; clock and reset are separate.
2. **Complete:** generate a port-specific SDC with no blanket
   `[all_inputs]`/`[all_outputs]` selectors. A digest-bound, fail-closed
   audit now rejects artifact drift, missing/stale port coverage, and missing
   clock/reset declarations before any expensive run. Evidence:
   `docs/evidence/phase3-ibex-constraints/constraints.json` and
   `ibex_core.sdc`.
3. **Complete:** deterministic, fail-closed parsing and policy now cover
   unconstrained functional endpoints, max slew/capacitance, high-fanout
   diagnostics, floating and overdriven nets, setup/hold, DRC, and antenna.
   Empty DRC/antenna reports require a completed run record with explicit zero
   counts. The ANTENNA_24/DIODE classification is exact, report-matched, and
   proven against the hashed physical netlist. Reports must live under the
   hashed manifest job root; the run record cross-checks RTL, manifest, tools,
   and target. Evidence:
   `docs/evidence/phase3-ibex-baseline-physical-gate.json`.
4. **Historical first attempt, failed closed:** the exact reviewed SDC (SHA-256
   `4f9f6ac1...`) was consumed at 20 ns with the pinned converted Ibex RTL
   (SHA-256 `068c27a0...`) and AREA3. Synthesis, floorplanning, placement and CTS
   completed. Global routing stopped with **46 total overflow** at 64.50%
   aggregate routing usage (met1 72.58%, met2 78.29%). The terminal routed-net
   count was 10,099 with 515,243 um wirelength and 73,011 vias. Because detailed
   routing never ran, final timing/electrical, DRC, antenna, GDS and physical
   netlist evidence do not exist. The incomplete candidate is not promoted.
   No retry and no constraint relaxation were performed. Durable record:
   `docs/evidence/phase3-ibex-reviewed-pnr-20ns-v1.json`.
5. **Bounded routability run completed:** a second, explicitly authorized run kept
   the pinned RTL, reviewed 20 ns SDC, AREA3 strategy and Sky130 demo target. It
   changed only physical density controls: 30% floorplan utilization and 0.50
   placement density. Global and detailed routing completed, OpenROAD reported
   zero detailed-route DRC and zero remaining antenna violations, and final GDS,
   SPEF, three-corner SDF, LVS and LEC netlists were emitted. Durable records:
   `docs/evidence/phase3-ibex-routability-p30-v2.json` and
   `docs/evidence/phase3-ibex-routability-p30-v2-physical-gate.json`.
6. **Physical gate evaluated, failed closed:** extracted final timing is −2.33 ns
   setup, −0.03 ns hold and 44.79 MHz Fmax. The final electrical report contains
   791 max-slew violations. Floating nets, overdriven nets, functional
   unconstrained endpoints, OpenROAD detailed-route DRC and antenna counts are
   zero. Maximum reported fanout is 32.
7. **Post-route equivalence attempted:** the functional Verilog cell model is
   incompatible with the local Yosys parser. The exact Liberty retry completed
   inconclusively with 25,700 comparison cells unproven. Both attempts and hashes
   are preserved under `docs/evidence/phase3-ibex-routability-p30-v2-equivalence*/`.
   No functional counterexample was reported. SDF files exist; gate-level SDF
   simulation lacks a qualified Ibex integration testbench and compatible timing
   cell models, so it remains unexecuted. Independent KLayout DRC and LVS were
   outside this SiliconCompiler ASIC flow and remain open.

## Phase boundary

The reviewed-constraint Phase 3 candidate now completes placement, CTS, global
routing, detailed routing and view generation. The routability blocker is closed.
Physical acceptance remains open because setup, hold and max-slew checks fail.
Post-layout equivalence is inconclusive; SDF simulation, independent KLayout DRC
and LVS remain open.

No 50 MHz closure, manufacturability, signoff, or provider acceptance is claimed.
