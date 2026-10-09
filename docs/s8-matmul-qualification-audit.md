# S8 matmul3 qualification evidence audit

Audit date: 2026-10-04. The initial section records persisted evidence; later
updates explicitly identify the independent simulation and bounded EDA run.

## Contract and recoverability

The preserved project is `proj-97b597f0490a`. Its frozen contract is
`matmul3-qualification/v1`, top `systolic_mm_4x4`, Sky130
`skywater130_demo`, 50 MHz / 20 ns, asynchronous active-low reset, exact
10-cycle latency and 10-cycle initiation interval. The contract hash recorded
by the passing r7 simulation is
`ebc409fe4806f06e5568c885fb2160b82a9276b272e763e4c99a9b5ec1851b19`.

The persisted history records these exact RTL hashes and jobs:

| revision | RTL SHA-256 | functional evidence | timing evidence |
| --- | --- | --- | --- |
| r5 | `5eb245f99ec6ff1bad53626f53c61aea73d9087768ef3be67f24d7e7f3343023` | not independently recoverable from the project snapshot | `syn-005650d6`, setup slack −1.16 ns, Fmax 47.25 MHz |
| r6 | `b0ba38da694cb3119dc0ddf2150712fef0ceb97400fb6df9c904383a349f42bf` | not independently recoverable from the project snapshot | `syn-a9c35653`, setup slack −3.63 ns, Fmax 42.33 MHz |
| r7 | `8b8d6785fb841a243aed64de77cbc6e0251d08df02145309811e275fc42f3063` | `sim-00067a06`, passed all five frozen checks | no timing job completed |

The job logs preserve commands, metrics, timing-path reports and artifact
handles. A follow-up byte-exact check of the project's `latest_verilog.code`
(without adding a command-output newline) produced
`8b8d6785fb841a243aed64de77cbc6e0251d08df02145309811e275fc42f3063`.
This exactly matches the recorded r7 RTL identity. The earlier `7ce950…`
value in this audit was a shell-measurement error caused by hashing an added
newline; it is superseded.

The recovered source is now preserved as
`recovered-current-8b8d6785fb841a24`, both as a standalone candidate and in
the content-addressed evidence store. Three independent Icarus trials, each
using 40 deterministic frozen-contract vectors and the human-authored Python
reference, passed exact arithmetic, protocol and 10-cycle latency checks. The
qualification manifest is
`backend/data/qualifications/matmul3/matmul3-20261004T134829.354996Z-8b8d6785fb84/manifest.json`.
The historical model-authored testbench remains unavailable, but it is not
used by this independent qualification.

Conclusion: r5/r6 timing numbers remain historical evidence without recovered
source payloads. The exact r7 RTL source is recovered by digest and has fresh,
independent functional/latency evidence. A fresh 20 ns Sky130 screen measured
setup slack −3.03 ns and Fmax 43.43 MHz, so it does not meet 50 MHz. The
manifest is `backend/data/qualifications/matmul3/matmul3-20261004T180854.960513Z-8b8d6785fb84/manifest.json`.
No source was reconstructed from prose.

After correcting `.env` precedence, the single bounded autonomous repair used
the project-selected direct Kimi provider. It consumed 53,515 build-run tokens
(61,513 including contract planning), produced a clean-lint RTL candidate, and
failed its first simulation because rows/results were misaligned. The 60k
admission bound then stopped round 4 before another paid generation; zero
synthesis calls ran and no failed candidate became the verified baseline.

The final minimal paid end-to-end smoke passed through REST + WebSocket on a
new `kimi-k3` project: persisted response exactly `SMOKE OK`, zero tool calls,
completed lifecycle, 3,262 provider tokens. The initial smoke checker ignored
`text_delta` frames and reported a false negative; the persisted transcript
and run ledger are authoritative, and the checker now handles both frame forms.

## Focused Kimi repair experiment

A separately authorized focused experiment resumed from the exact lint-clean
`c2ec673b…` repair artifact rather than opening another autonomous loop. One
direct Kimi K3 completion consumed 10,437 provider tokens (2,568 prompt and
7,869 completion, including 5,285 reasoning tokens). Kimi correctly diagnosed
the extra `v3/r3` delay and made only the corresponding `v2/r2` write-tag fix.

The repaired `33e304e56e97…` candidate passed three independent trials of the
frozen 40-vector functional/protocol/latency qualification. Evidence is in
`backend/data/qualifications/matmul3/matmul3-20261004T190829.974421Z-33e304e56e97/manifest.json`.
The single permitted 20 ns Sky130 screen then measured setup slack −28.11 ns
and Fmax 20.78 MHz. Its critical path is the 256-load `launch_en` product-register
enable network rather than the repaired result-tag path. Evidence is in
`backend/data/qualifications/matmul3/matmul3-20261004T190839.808185Z-33e304e56e97/manifest.json`.

The candidate is therefore rejected for timing and was not promoted. The
byte-exact `8b8d6785…` candidate remains the verified baseline at 43.43 MHz /
−3.03 ns. No further paid call or synthesis retry was made.

## Next bounded action

The measured baseline critical path was optimized without paid model calls.
Every candidate passed three independent frozen functional/protocol/latency
trials before receiving one 20 ns Sky130 screen:

| candidate | change | Fmax | setup slack | decision |
| --- | --- | ---: | ---: | --- |
| `8b8d6785…` | recovered r7 | 43.43 MHz | −3.03 ns | superseded |
| `d049bd18…` | isolate multiplier outputs | 46.36 MHz | −1.57 ns | superseded |
| `2dc1e3e6…` | additionally split 17-bit pair add | **47.85 MHz** | **−0.90 ns** | new engineering baseline |
| `895ceebd…` | additionally split 18-bit final add | 46.68 MHz | −1.42 ns | rejected; area/load regression |

The new baseline source is
`backend/data/qualifications/matmul3/candidates/baseline-split-pair-adder/systolic_mm_4x4.sv`
and is also preserved under its full SHA-256 in the evidence artifact store.
Its functional manifest is
`backend/data/qualifications/matmul3/matmul3-20261004T191801.087402Z-2dc1e3e64e23/manifest.json`;
its timing manifest is
`backend/data/qualifications/matmul3/matmul3-20261004T191811.773725Z-2dc1e3e64e23/manifest.json`.

The apparent 0.90 ns miss exposed a flow-policy defect rather than another RTL
requirement: the Yosys `syn_asic` task carried a 20 ns ABC period but left its
supported strategy empty. The screening runner now selects the allow-listed,
explicit `DELAY1` strategy through SiliconCompiler's public task API and records
it in the machine-readable `SCJOB` result. Arbitrary ABC scripts are rejected.

Re-screening the unchanged `2dc1e3e6…` RTL produced **+0.34 ns setup slack,
50.86 MHz Fmax, +0.05 ns hold slack, zero TNS and zero unconstrained paths**.
Cell area is 102,062.89 µm², with 16,094 cells, 1,389 registers and a 13.1 mW
reported power estimate. The passing manifest is
`backend/data/qualifications/matmul3/matmul3-20261004T192727.831146Z-2dc1e3e64e23/manifest.json`.

The frozen matmul3 50 MHz **screening** gate is therefore closed. This screen
remains pre-layout demo-PDK synthesis+STA evidence; the separate routed result
below must not be conflated with its manifest.

## Single full place-and-route run

The exact `2dc1e3e6…` candidate was run once through LibreLane 3.0.14 Classic
with Sky130 PDK revision `8afc8346a57fe1ab7934ba5a6056ea8b43078e71` and a
20 ns clock. The isolated run is preserved under
`backend/data/qualifications/matmul3/pnr-2dc1e3e64e23/runs/qualified-2dc1e3e64e23/`.

- Flow exit: 0; final GDS and LEF produced.
- Worst multi-corner setup slack: **+2.389 ns**; setup TNS and violations: 0.
- Worst multi-corner hold slack: **+0.103 ns**; hold TNS and violations: 0.
- Die area: **433,633 µm²**; standard-cell area: **162,084 µm²**;
  standard-cell count: **18,983**; sequential cells: **1,389**.
- Estimated total power at nominal TT: **82.62 mW**.
- Routing, Magic and KLayout DRC errors: 0; antenna violations: 0; LVS
  differences/errors: 0; stream-out XOR differences: 0.

This is useful physical-implementation evidence, and a clean release or
signoff gate is still beyond it. LibreLane used its generic fallback PNR/signoff SDC rather than
the screening SDC; it reports 2,821 max-slew violations, 10 max-cap violations,
20 max-fanout violations, two floating nets and 446 lint warnings. Optional
post-layout EQY was disabled, and IR-drop used default source placement. Tiny
Tapeout wrapper integration/provider hardening and foundry signoff remain
separate work.
