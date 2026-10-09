# Phase 2 known-core qualification report

Date: 2026-10-07; status updated 2026-10-09
Outcome: Phase 2 is complete with a bounded Ibex qualification baseline. Physical closure, post-layout equivalence, and signoff remain later-phase work.
No model completion or reference-core source modification was used.

## Frozen references

| Role | Core | Upstream commit | License | Manifest digest |
| --- | --- | --- | --- | --- |
| Control | PicoRV32 | `ef203c2b0a3fb793280f5114941416c425c5b461` | ISC | `52643fb6dd7f1cb5ed44425b2adbe06fd5e7e48ced4a2a1ffeb4cdee2e42e2bb` |
| Multi-file target | Ibex minimal core | `654ac71f91f7da5dc71942c9ba8d3103666c484e` | Apache-2.0 | `367870e1a90f85b1b82ed2d067c26154b0dbfee0e9c95abc302ee9806a03c394` |

PicoRV32 is the calibration control. Ibex is the Phase 2 qualification target.
The EcrioniX and EkthaReddy designs remain educational/decomposition references,
not correctness anchors.

## Implemented platform boundary

- The hardware-project manifest now carries import provenance.
- A trusted resolver materializes verified content-addressed artifacts beneath a
  digest-specific directory.
- Compile order, include directories, preprocessor defines, source roles and top
  module are passed to Verilator without source concatenation.
- Missing/corrupt artifacts, unsafe paths and symlink materialization targets
  fail closed.
- The FuseSoC importer stores include-only macro files as artifacts even when
  they use a `.sv` suffix; they remain headers and are not compiled twice.
- Qualification reports bind the exact source manifest, tool version, ordered
  source list, diagnostics and report digest.
- The source browser shows compact upstream role/commit provenance while
  preserving the existing workbench direction.

## Executed evidence

### PicoRV32 control

- Verilator 5.032 lint/elaboration: completed with exit 0 and 34 warnings.
  This proves elaboration; production lint acceptance comes from the
  policy-driven lint gate.
- Upstream `testbench_ez.v`: Icarus compilation exit 0 and simulation exit 0.
  The bench reaches its expected `$finish`, but it has no explicit
  self-checking PASS marker and is recorded as a smoke test, with ISA compliance covered separately by the
  pinned compliance suite.
- The initial Sky130 20 ns `DELAY1` screen stopped in the final Yosys ABC pass
  with return code 134. The first `abc -fast` pass and an AREA-oriented mapping
  both completed, isolating the failure to an ABC 1.01 internal assertion in
  the delay strategy (`abcAig.c:1134`, `Abc_ObjIsNode(pNode)`); the PicoRV32
  RTL itself is unaffected.
- The documented `AREA3` fallback completed synthesis and STA at 20 ns:
  setup slack **+0.53 ns**, hold slack **+0.02 ns**, Fmax **51.37 MHz**,
  **10,063 cells**, cell area **85,375.63 um2**, power **4.59 mW**, and zero
  unconstrained endpoints. These are screening metrics; signoff belongs to a later assurance level.
- Successful durable SC manifest:
  `/mnt/cold-hdd/sc_builds/phase2-picorv32-area3/picorv32/job0/picorv32.pkg.json`.
- Failed-strategy manifest:
  `/mnt/cold-hdd/sc_builds/phase2-picorv32-screen/picorv32/job0/picorv32.pkg.json`.

### Ibex target

- Native pinned FuseSoC `lowrisc:ibex:ibex_core:0.1` lint target: exit 0.
- Digest-bound manifest lint: exit 0 with 83 non-fatal diagnostics.
  This proves the resolved multi-file SystemVerilog graph elaborates; warning
  policy acceptance remains open.
- The `ibex_top` lint target at this upstream commit overrides its inherited
  fileset and omits the Ibex sources, so the minimal `ibex_core` target is used
  deliberately. The reference source was not patched.
- Open-source FuseSoC 2.4.7 is pinned in backend dependencies.
- Content-addressed sv2v preprocessing is integrated behind a pinned container:
  `0x01be/sv2v@sha256:6526715992bc1ccc75c016beb4aed4ca0aa80ab0a4b7669d93073222e36de658`
  (`sv2v ddaa7ff`). The converted Verilog SHA-256 is
  `8b435d0d457551fd2058ff175eb8fc23fc0b6494abc276eacab91e39cb92e6f6`;
  the conversion report digest is
  `eb078882b2c9e57bda946de72ba9c109c6aa29d6dec3b87e031dc7dd35295f93`.
- Physical synthesis and RVFI/formal are now separate digest-visible execution
  profiles. The synthesis profile removes only the verification-only `RVFI`
  define and retains `SYNTHESIS`; upstream RTL is unchanged. This removes the
  RVFI array elaboration failure instead of patching generated RTL.
- The synthesis/formal profiles now specialize the pinned top before sv2v and
  fail closed if its reviewed parameter declarations drift. The measured
  configuration is RV32I + Zca with `RV32M=None`, `RV32B=None`, `RV32E=0`,
  `DbgTriggerEn=0`, and `SecureIbex=0`. This revision has no `RV32ZC=None`
  setting, so a strict no-compressed RV32I claim would be false.
- The synthesis-profile sv2v artifact elaborates and synthesizes. A corrected
  20 ns AREA3 screen on `clk_i` completed with zero unconstrained endpoints:
  setup slack **-14.33 ns**, hold slack **+0.03 ns**, Fmax **29.13 MHz**,
  logic depth **42**, **15,021 cells**, cell area **91,256.27 um2**, and
  estimated power **9.72 mW**. The 50 MHz target fails. These are screening
  metrics; P&R and signoff remain ahead.
- The specialized 20 ns AREA3 screen also completed with zero unconstrained
  endpoints: setup slack **-11.01 ns**, hold slack **+0.03 ns**, Fmax
  **32.25 MHz**, logic depth **38**, **8,967 cells**, **804 registers**, cell
  area **61,277.52 um2**, and estimated power **7.34 mW**. It improves the
  upstream-default screen but still fails 50 MHz. This is the current physical
  screening baseline; it is not P&R or signoff.
- Native-to-converted equivalence is proven for the exact specialized manifest
  and converted digest. Gold was independently elaborated with
  sv-elab/`read_slang`; gate used Yosys `read_verilog`. Symmetric `async2sync`
  and `memory_map` normalization made asynchronous resets and inferred ROMs
  representable to the equivalence engine. All **15,730 / 15,730** comparison
  cells proved; no unproven cells or counterexample remain. The pinned
  environment is LibreLane 3.0.14 with Yosys 0.62 and its sv-elab plugin.
- A separate manifest-native RVFI formal profile is now digest-bound as
  `b7fadbf9282db8ca95d357f4951005e7e164d856b501898d1ac16effd3ca4f83`.
  Native SystemVerilog is elaborated with the pinned LibreLane 3.0.14
  sv-elab/Yosys 0.62 environment; upstream source remains unchanged and a
  separately hashed checker is inserted only in the formal work artifact.
- At depth 12, all bounded interface-consistency assertions held: RV32 IXL,
  retired-PC halfword alignment, mutually exclusive RVFI memory read/write
  masks, zero x0 destination data, and monotonically increasing retirement
  order. An independent SAT reachability run found `rvfi_valid` at the final
  bound, so the result is not vacuous. The report digest is
  `4d758179aec0b749e24705e7189bbc1af9f493a8ff0e7e1fc9353ec58047f4d4`.
  This is bounded interface evidence; ISA compliance and unbounded proofs
  remain outside it.
- The Z3 path is not claimed: Yosys's SMT2 exporter asserted on this native
  design. The successful recorded engine is Yosys SAT; the exporter defect is
  retained as a bounded platform gap rather than hidden.
- An earlier run accidentally constrained nonexistent port `clk`; its 41.02 MHz
  result and 1,429 unconstrained endpoints are rejected. Non-ANSI top-module
  clock detection now identifies `clk_i` and has regression coverage.
- Corrected-screen SC manifest:
  `/mnt/cold-hdd/sc_builds/phase2-ibex-synthesis-area3-clockfix/ibex_core/job0/ibex_core.pkg.json`.
- Specialized-screen SC manifest:
  `/mnt/cold-hdd/sc_builds/phase2-ibex-specialized-area3/ibex_core/job0/ibex_core.pkg.json`.
- Original RVFI-profile failed-screen SC manifest:
  `/mnt/cold-hdd/sc_builds/phase2-ibex-area3/ibex_core/job0/ibex_core.pkg.json`.

Detailed reports:

- `docs/evidence/phase2-picorv32-control.json`
- `docs/evidence/phase2-ibex-minimal.json`
- `docs/evidence/phase2-ibex-sv2v.json`
- `docs/evidence/phase2-ibex-synthesis-profile.json`
- `docs/evidence/phase2-ibex-synthesis-sv2v.json`
- `docs/evidence/phase2-ibex-synthesis-screen.json`
- `docs/evidence/phase2-ibex-specialized-profile.json`
- `docs/evidence/phase2-ibex-specialized-sv2v.json`
- `docs/evidence/phase2-ibex-specialized-screen.json`
- `docs/evidence/phase2-ibex-conversion-equivalence.json`
- `docs/evidence/phase2-ibex-formal-profile.json`
- `docs/evidence/phase2-ibex-formal-sv2v.json`
- `docs/evidence/phase2-ibex-rvfi-formal.json`

## Unmet qualification gates

1. Production warning policy has not classified or waived the upstream warnings.
2. Independent RISC-V architecture tests remain unavailable because a pinned
   RV32 cross-toolchain, reference simulator and compliance harness are not
   installed.
3. Bounded manifest-native RVFI interface properties pass non-vacuously at
   depth 12. Full riscv-formal instruction semantics and an unbounded proof are
   not integrated; the SMT2/Z3 exporter path remains blocked by a Yosys defect.
4. The PicoRV32 control passes the AREA3 synthesis+STA screen; the DELAY1 ABC
   assertion remains a tool-strategy defect to retain as regression evidence.
5. Ibex preprocessing, specialization, screening, and native-to-converted
   equivalence are digest-bound. The generic SDC clock-input-delay warning must
   still be removed by explicit Phase 3 constraints.
6. Consequently, neither a bounded Ibex P&R result nor a qualified-core baseline
   exists yet.

The Phase 2 exit criterion is satisfied only in its bounded-gap form: both
unchanged pinned cores have reproducible, digest-bound reports and every missing
gate is explicit. It is **not** a successful core qualification and does not
authorize Phase 3 physical-evidence closure yet.

## Next execution order

1. Install and pin an RV32 cross-toolchain, reference simulator and compliance
   harness under `/mnt/cold-hdd/riscv-qualification`; run independent ISA tests.
2. Apply the production lint policy to upstream diagnostics.
3. Run at most one bounded Ibex P&R only after lint, equivalence, ISA/formal and
   synthesis+STA screening gates pass.

## Closure update (2026-10-07)

This section supersedes the stale open items and execution order immediately
above; they are retained as the pre-closure record.

- Independent RV32I compliance now runs against the real specialized Ibex core
  using suite commit `844c6660ef3f0d9b96957991109dfd80cc4938e2`, the exact
  revision pinned by upstream Ibex CI. The lowRISC GCC archive is pinned by
  SHA-256 under `/mnt/cold-hdd` and used from there, without a system-wide
  install. **44/48** tests pass
  normally. The only failures are `I-EBREAK-01`, `I-ECALL-01`,
  `I-MISALIGN_JMP-01`, and `I-MISALIGN_LDST-01`: exactly the exclusions accepted
  by this pinned Ibex commit's CI. The result is recorded as success with known
  upstream exclusions, keeping the report away from a false 48/48 framing.
- The production lint policy fails closed on a changed manifest, count,
  severity, diagnostic code, or source-file class. Its reviewed baseline is 36
  `PINCONNECTEMPTY` findings at deliberate unused outputs and 47 `UNUSEDPARAM`
  findings in shared package files.
- After those gates passed, exactly one bounded 20 ns P&R invocation was made.
  It stopped during elaboration with `error: no input files` because
  SiliconCompiler did not infer Verilog from the extensionless content-addressed
  artifact path. Placement and routing did not run. This is an
  input-registration infrastructure failure in the tool flow; the Ibex design
  itself is unimplicated, and no second attempt was hidden.

New evidence:

- `docs/evidence/phase2-ibex-rv32i-compliance.json`
- `docs/evidence/phase2-ibex-production-lint.json`
- `docs/evidence/phase2-ibex-bounded-pnr.json`

Phase 2 is closed in its bounded-gap form. ISA and lint gates are now explicit
and machine-checked, but there is still no successful Ibex physical baseline,
so this does **not** authorize Phase 3 physical-evidence closure. The next action
is to make the runner register content-addressed RTL with an explicit Verilog
file type (and regression-test that boundary). Any second physical attempt needs
an explicit new authorization because the Phase 2 one-attempt budget is spent.

## Physical baseline follow-up (2026-10-08)

The explicitly authorized follow-up fixed the runner boundary by registering
content-addressed RTL as Verilog instead of relying on filename-extension
inference. A regression test now covers an extensionless SHA-style artifact.

One fresh run in an isolated build directory completed the full demo flow
through detailed routing, antenna repair, GDS export, extracted views, and IR
analysis. Final detailed-routing and antenna violations are both zero, and
there are no unconstrained endpoints. The resulting screening metrics are:

- setup slack **-2.31519 ns** at 20 ns (**44.8125 MHz** inferred Fmax)
- hold slack **+0.055722 ns**, setup TNS **-405.195 ns**
- **71,035.6 um2** cell area, **146,421.0 um2** die area, **49.4487%** utilization
- **11,379 cells**, **804 registers**, estimated peak power **10.1391 mW**

Evidence: `docs/evidence/phase2-ibex-physical-screen.json`.

Phase 2 now has a successful, reproducible physical **screening** baseline.
The 50 MHz timing target is not closed, and this demo-PDK result is not signoff.
Phase 3 may use it as the known-core baseline, but must address real constraints,
slew/capacitance/fanout policy, floating nets, and post-layout equivalence rather
than treating successful GDS generation as manufacturability proof.
