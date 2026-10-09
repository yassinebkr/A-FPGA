# Phase 2 known-core physical evidence

Status: bounded steps 1-4 executed; qualification exit remains open.

## Product goal and baseline policy

The product goal remains **50 MHz**. The current **24.07 MHz** DELAY1 result is
only a measured Sky130 screening baseline; it is not an accepted product
frequency and is not timing closure. No constraint was loosened to manufacture
a passing result.

## 1. Baseline audit

The exact SDC digest
`4f9f6ac174f92ed4e5f35ee69b3abf1a75c1d12f8f495fcb7b6a46efb82fdfc6`
was consumed by OpenSTA. The selected DELAY1 synthesis+STA result is:

| Metric | Result |
| --- | ---: |
| Setup slack | -21.54 ns |
| Hold slack | -0.18 ns |
| Total negative setup slack | -4371.68 ns |
| Fmax | 24.07 MHz |
| Cell area | 58,359.72 um2 |
| Cells | 8,521 |
| Registers | 804 |

The detailed reports expose evidence that the compact metric hid:

- 736 max-slew, 5 max-fanout, and 6 max-capacitance violations;
- an unconstrained `data_gnt_i -> _10333_/A` path;
- a control-enable cone with fanout 83, 0.24 pF capacitance, and 16.37 ns
  transition;
- reset fanout 531 and 2.00 pF capacitance.

The evidence collector now parses detailed unconstrained reports as well as the
compact summary, and accepts OpenSTA fanout rows whose slack column is blank by
deriving slack from the limit and measured value. Missing or malformed evidence
still fails closed.

The prior routed result remains reclassified as non-clean evidence: it has 274
driver-rule violations and one unwaived `ANTENNA_12/DIODE` unconstrained
endpoint. It is not comparable proof of 50 MHz closure.

## 2. Minimal configuration and bounded candidate

The existing converted artifact already specializes `RV32M=None`,
`RV32B=None`, `RV32E=0`, `DbgTriggerEn=0`, and `SecureIbex=0`.
It still contains the minimum-supported `RV32Zca` and sources from the pinned
CHERIoT-era tree, so it must not be described as strict, residue-free RV32I.

A separate immutable timing candidate enabled Ibex `WritebackStage=1` while
keeping `BranchTargetALU=0`. Its conversion/source digest is
`fbf729cc4ceefaa973b89a83091f79aa7f9880bcc591a5f0b9e36999a233d3e3`;
the specialization manifest digest is
`9bc470e26ddc6a9f4b1987f4f93acb0eb3930ffb27889238e28a9ba83362797b`.
The profile and conversion evidence are preserved under `docs/evidence/`.

Source audit rejected the previously proposed `7b5df75a...` revision because
it still contains `rtl/ibex_cheriot_pkg.sv`. Its verified direct ancestor
`e4bcf74971b3eef89f1f2c85559784608d28de52` is the actual pre-CHERIoT
qualification target. It remains RV32I+Zca because that revision has no
`RV32ZCNone` configuration; it must not be labelled strict RV32I.

## 3. Critical-path diagnosis

The baseline critical path is a control/enable path; the multiplier and ALU
datapath lie outside it:

`csr_addr[6] -> decode/control -> if_id_pipe_reg_we -> crash_dump_o[129]`.

`crash_dump_o` aliases IF/ID pipeline state. Synthesis consumes the electrical
constraints for reporting, but the current mapping does not repair the
post-mapping high-fanout flop-enable and reset networks. A more aggressive
DELAY4 mapping regressed to 20.75 MHz.

RTL-to-synthesis equivalence was rerun with functional Liberty loading and a
symmetric, explicitly recorded `async2sync` abstraction. The proof scope is
clock-edge behavior; asynchronous assertion between edges is outside it. The
result remains **inconclusive**: 158 cells proved and 309 remained unproven.
This is more precise than the previous generic asynchronous-FF blocker, but it
is not equivalence success.

## 4. Bounded 50 MHz closure attempt

The DELAY1 run for the writeback-stage candidate hit the known ABC internal
assertion, so one AREA3 synthesis+STA fallback was used:

| Metric | Result |
| --- | ---: |
| Setup slack | -12.75 ns |
| Hold slack | -0.17 ns |
| Fmax | 30.54 MHz |
| Cell area | 63,589.74 um2 |
| Cells | 9,109 |
| Registers | 878 |
| Peak power estimate | 6.92 mW |
| Logic depth | 38 |

This improves measured synthesis Fmax by 6.47 MHz (about 26.9%) over the
24.07 MHz baseline, but misses 50 MHz. It also reports 754 slew, 6 fanout, and
6 capacitance violations plus an unconstrained
`data_gnt_i -> _08368_/A` path. Its critical cone now starts at
`rf_raddr_b_o[0]` and ends in the IF-stage FIFO error register.

The candidate was therefore **not promoted**, and the one permitted P&R run
was not launched. P&R admission requires clean/classified constraints,
electrical evidence, and an acceptable equivalence result; spending it now
would only produce another non-qualifying headline.

## Pre-CHERIoT qualification result

The corrected source target was frozen as an immutable 89-source project with
`WritebackStage=1`. Native Verilator elaboration passed, pinned offline sv2v
conversion succeeded, and independent native-SystemVerilog versus converted-
Verilog equivalence was proven.

The exact 50 MHz DELAY1 synthesis+STA screen measured:

| Metric | Result |
| --- | ---: |
| Setup slack | -19.39 ns |
| Hold slack | -0.18 ns |
| Setup TNS | -3651.49 ns |
| Fmax | 25.39 MHz |
| Logic depth | 26 |
| Cell area | 61,251.24 um2 |
| Cells | 8,791 |
| Registers | 875 |

The corrected top-level constraint contract produced zero unconstrained paths,
but the screen still has 892 slew, 6 fanout, and 6 capacitance violations. The
critical path is again an internal control-enable cone from
`id_stage_i/controller_i/instr_i[3]` to `crash_dump_o[129]`; its final
`if_id_pipe_reg_we` driver has fanout 83, 0.24 pF capacitance, and 16.37 ns
transition.

Post-synthesis equivalence remains inconclusive with 309 unproven cells under
the recorded symmetric asynchronous-reset abstraction. The candidate therefore
was not promoted. The best measured synthesis candidate remains the newer-tree
writeback configuration at 30.54 MHz, and no P&R was admitted.

Canonical evidence:
`docs/evidence/phase3-ibex-pre-cheriot-qualification.json`.

## Five-gate closure campaign and second-core control

The follow-up gates were executed without changing the 50 MHz requirement or
patching either upstream core. For Ibex, the bounded configuration space already
covered the unmodified pipeline and `WritebackStage=1`, plus AREA3, DELAY1 and
DELAY4 mapping attempts. The best preserved Ibex result remains 30.54 MHz. Its
critical failure is internal control/enable distribution, so no additional
configuration-only change can honestly be called a repair.

The pinned PicoRV32 control was then re-screened under an explicit, port-complete
20 ns contract instead of the older fallback SDC. The AREA3 result closes setup:

| Metric | Result |
| --- | ---: |
| Setup slack | +1.49 ns |
| Hold slack | -0.17 ns |
| Setup TNS | 0.00 ns |
| Fmax | 54.03 MHz |
| Logic depth | 10 |
| Cell area | 85,375.63 um2 |
| Cells / registers | 10,063 / 1,597 |
| Unconstrained endpoints | 0 |

This isolates the platform result: the open synthesis/STA stack can map a known
RV32I core above 50 MHz under the reviewed interface contract. It does **not**
qualify the core physically. The mapped screen still contains 2,519 max-slew,
4 max-fanout and 40 max-capacitance violations, and negative hold slack. Those
pre-placement networks require physical buffering/hold repair; hiding them with
looser limits is forbidden.

RTL-to-mapped-netlist equivalence was also exercised. The runner no longer
misclassifies inactive preprocessor macro names containing `BLACKBOX`, and it
now normalizes memories symmetrically before comparison. The exact proof still
ends with 145 unproven cells. A bounded follow-up enabled Yosys undefined-state
modeling; it left the same 145 cells unproven. A reset-aware output miter then
showed that separately loaded RTL and gate models begin with independent values
for PicoRV32 registers that are intentionally not reset. This invalidates the
external-netlist miter as a conclusive synthesis-equivalence method for this
core: it does not establish the required pre/post state correspondence. The
remaining cone includes look-ahead memory outputs, PCPI operands and `trap`.
No equivalence pass or functional counterexample is claimed. The required next
proof is transformation-aware equivalence inside the exact Yosys synthesis
sequence, or another sequential-equivalence engine with an explicit reset and
state-correspondence model.

A bounded in-process Yosys `equiv_opt` experiment was then run from the exact
source digest `0836050971b3c6cdd28ac3b1e5719a67fb645161912bef1e472e63995ceb0622`.
The production runner now stages and digest-binds the transformation script,
rejects boundary-changing commands, creates both designs in one Yosys process,
and only accepts an explicit terminal proof. A parser regression found during a
tiny sequential smoke proof was fixed: an intermediate induction workset can no
longer override a later zero-unproven `Equivalence successfully proven` status.

On PicoRV32, generic flattened synthesis remains inconclusive: `-undef` leaves
3,028 comparison points unproven (report digest
`59787c0cfdc4aceeb6e6f30c56a9bcb87b18433db03a9de52c83a3ec8b717af4`). A
controlled run without `-undef` reduces that to 431 but does not prove the
design (report digest
`4a79f5740817484fc1d3c5fdfaa0e2a36b3eaaf5bc617b734b7ffdc8b923e9e2`). Both
bind transform digest
`46729acb99424f99f74095b3e1a944e88312f98a65beb9c4b3d6ce2199e060b0`.
This is inconclusive methodology evidence: no counterexample was produced,
and the required digest-bound proof of the exact Sky130-mapped candidate is
still missing. Replaying the
library mapping cannot be admitted as the qualification proof until reset/state
correspondence closes at the generic transformation boundary.

Accordingly, the physical implementation gate was evaluated, and admission
was withheld:
setup closure alone is insufficient while hold, electrical, and equivalence
gates remain red. The five requested gates therefore end in a bounded platform
control result, and P&R promotion is withheld. Canonical evidence is
`docs/evidence/phase3-picorv32-control-qualification.json (legacy immutable evidence ID)`.

## Remaining exit criteria

- Qualify a genuinely minimal, pinned core target without silently changing
  the ISA or interface.
- Constrain or narrowly classify every real interface path, including
  `data_gnt_i`.
- Repair enable/reset slew, fanout, and capacitance rather than relaxing limits.
- Obtain digest-bound RTL-to-synthesis equivalence, then synthesis-to-route
  equivalence where supported. For PicoRV32, use transformation-aware
  equivalence; do not promote an independently initialized cross-file miter.
- Admit at most one P&R run for a candidate that passes those gates.
- Close 50 MHz post-route, or preserve the best verified candidate and publish
  a bounded critical-path report.
- Add SDF gate-level simulation as complementary evidence.

The canonical machine record is
`docs/evidence/phase3-ibex-physical-evidence.json`.
