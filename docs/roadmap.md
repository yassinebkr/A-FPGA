# Engineering roadmap and status

Status date: **2026-10-13**

This roadmap records the execution order and the evidence boundary. The
platform is local-only and single-user. Open_MC owns product implementation
and releases in its private repository. This repository owns reusable tooling
and qualification evidence bound to exact revisions.

## Strategic milestone

Carry an open, modifiable RV32 microcontroller from specification through
verification, physical implementation, manufacturing preparation, packaged
bring-up, and documented silicon revisions.

- Product target: 250 MHz.
- Stretch objective: 500 MHz, subject to technology and physical evidence.
- Leading physical candidate: IHP SG13G2, pending provider and MPW review.
- Later milestone: Linux-capable RV64 around 1 GHz.

## Phase table

| Phase | State | Evidence and next boundary |
| --- | --- | --- |
| 1. Architecture-first generation | Complete | Hashed architecture contracts gate RTL and EDA. |
| 2. Deterministic timing optimization | Complete | Measured critical-path retries preserve contract and function. |
| 3. Matmul qualification | Complete, bounded | Repeated functional/protocol trials, 20 ns Sky130 screen, and one routed engineering run. The separate physical gap audit records the limits. |
| 4. FPGA target flow | Later track | Select a board, add constraints/programming, and obtain hardware-in-the-loop evidence. |
| Open_MC product qualification | Active | Close architectural, physical, and core-selection gates below. |

The Sky130 Phase 3 milestone is closed as a bounded engineering result. Its
physical acceptance fails on setup, hold, and max-slew. See
[docs/phase3-physical-gap-audit.md](phase3-physical-gap-audit.md). New physical
experiments require an explicit hypothesis and evidence budget.

## Global checkpoint — 2026-10-13

The A-FPGA platform has working generation, persistent projects and evidence,
bounded verification, asynchronous EDA jobs, multifile RTL support, netlist and
physical views, and completed C0–C6 visualization qualification. Remaining
platform polish includes project import/export, job and metric presentation,
physical visualization depth, and general UX finish.

Open_MC has a ten-patch CV32E40P baseline with reproduced ACT4 RV32IMC,
Zicsr, Zifencei, PMP and `InterruptsSm` evidence; bounded Sail, RVFI, CSR/fence,
synchronous-fault, PMP access-fault and interrupt-lifecycle evidence; 129 unit tests; and
documented reproducible artifacts. The first physical-mapping attempt exposed an
unsynthesizable `while` loop inside the patch-0003 PMP `region_matches` function;
patch 0003 is revised to a statically bounded loop
([patch-0003 synthesis repair](evidence/open-mcu-cv32e40p-patch0003-synthesis-repair.json),
new SHA-256 `12f9d9f0e3dca11f476da86d62ef998c49f9745d2a51287b632f2d5cfa3f22e5`)
and the PMP-, fault-, and formal-evidence lanes are refreshed on the revised
profile. The 250 MHz product objective remains subject to selected-technology
post-route evidence; 500 MHz is a technology-dependent stretch objective.

Ordered continuation gates:

1. Extend the closed seeded campaigns with randomized PMP configuration,
   delayed/simultaneous bus faults and additional compressed encodings.
   Closed at the screen level: the seeded campaign covers these classes 36/36
   on the revised ten-patch profile.
2. Add broader functional coverage, assertions and formal properties.
3. Complete production lint, CDC/RDC and wider equivalence.
4. Refresh physical qualification with the ten-patch baseline. Closed at the
   mapped and placed screening level (100 MHz met); routed closure remains
   gate 5.
5. Close timing, slew and fanout, then complete DRC/LVS and SDF post-layout simulation.
6. Select the final core and technology from reviewed evidence.
7. Integrate the complete SoC: memories, UART, SPI, I²C, boot and programming.
8. Run an FPGA prototype when hardware budget is available.
9. Complete packaging and manufacturing preparation.

The later milestone is a Linux-capable RV64 design around 1 GHz, after the
RV32 product and its manufacturing evidence.

## Open_MC active gates

Passed gates for the repaired CV32E40P profile:

- ACT4 RV32IMC, Zicsr, and Zifencei: 80/80, reproduced byte-identically.
- Privileged PMP: 45/45 twice. PMPSm: 33/33. PMPZca: 12/12.
- Interrupt harness: 11/11 twice. It covers entry, masking, direct/vectored
  dispatch, `mret`, acknowledgement, simultaneous-source priority, and
  entry-time `mstatus`/`mie`/`mip`.
- ACT4 `InterruptsSm-00`: 1/1 twice with byte-identical hash
  `d5f9be65fbdca4f2926a685edda2342fb7037db548db1a6848e612f4f7c1c69d`.
- Independent interrupt semantic comparison: 11/11 twice with byte-identical
  hash `eb4b948c1f50f1b056ba08064fc81cdee313cb1b930ee6b36cfe02727f3cf4dc`.
  Deterministic assertion timing passes 28/28 twice across four seeds under
  hash `fc9c7df85cad4064036ff0ce8f76c2ed4717daa68e9209d29691f040178921d8`.
  Fast interrupts pass 6/6 twice for causes 16, 23, 31 and priority conflicts
  under hash `ce36b843e11f741a259a0188054fafdba36dc268bad7ada6b93867b8b328482d`.
  The complete line sweep and four lifecycle cases pass 20/20 twice under
  byte-identical hash
  `c8fd1714dc0c623aadcd1f2745b6116dc29cd3fdd6ed2897b2482b5927ff457f`.
  Controlled two-level nesting passes 4/4 twice under byte-identical hash
  `ef9b0771c1904ee80b7e50acb5a38796338d94a3e08c02f06268fd57edcf76d2`.
  Its deterministic timing matrix passes 16/16 twice under byte-identical hash
  `15b8246a8c1377301573fbbb7bb077be4218c5f6c595e765282cbff4210c7f1e`;
  the independent comparison passes 16/16. Deeper nesting, sub-cycle phase
  behavior, and CDC/RDC closure remain open. The first conservative structural
  audit fails closed: 19 interrupt inputs have one sampling stage, the raw
  interrupt bus drives wake-up logic, and reset deassertion remains unqualified.
  A reviewed MCU-boundary synchronizer and reset-release architecture is the
  next required repair. That boundary is now implemented and qualified in
  isolation: selected asynchronous level IRQs use two marked stages, reset
  releases through two stages, and wake indication uses synchronized state.
  MCU-top integration and netlist-aware CDC/RDC remain open.
  The first `openmc_mcu_core_top` now elaborates the complete repaired core
  through this boundary with a frozen IRQ map. Its two runs are byte-identical
  under manifest `54d410244aa46cad1046656a9fac78782124594243af1a2712cead78001f8d3d`.
  Memory/interconnect integration and firmware execution through this top are
  the next functional gate. The first executable slice now closes that bounded
  gate: an 8 KiB ROM firmware reaches the repaired core and writes signature
  `0x5a` through data OBI into the 64 KiB SRAM fallback at cycle 9. Two runs
  are byte-identical under manifest
  `9c1d1e0d33e608fc7df223c7f06592664ed77f367eb8bf115a1d6a88f9d499ec`.
- Sail differential: 16 deterministic seeds × 128 operations against Sail
  0.13.1 from the pinned ACT4 image.
- Synchronous-fault mtval repair: **3/3 twice** for illegal instruction,
  misaligned word load, and misaligned word store. Observed `mtval` values are
  `0xffffffff`, `1`, and `1`; `mcause`, `mepc`, one trap, `mret` resume, and
  signatures match. Patch 0007 SHA-256 is
  `7ced118f05a0cf90e23afe386c2c9c54309c4576e8c3eac9201aebce369b39c4`; the
  byte-identical manifest is
  `29b77a98edb1a48ddcb737eac6330f4d18db9772df748a2f6e8e62d5a167b4fe`.
  Unsupported mtval trap classes remain open. Directed repaired-profile
  coverage is 15/15; RVFI has 18 records and zero reference mismatches; 62
  nested unit tests pass.
- PMP access-fault differential: **3/3 twice** on the repaired eight-patch
  profile. Load/store denials at `0x2000` report `mcause=5/7`, `mepc=0x24`,
  `mtval=0x2000`; instruction denial at `0x80` reports `mcause=1`,
  `mepc=0x80`, `mtval=0x80`. Each case records one trap, the expected
  signature (`5`, `7`, `1`), and zero denied bus requests. Patch 0008 SHA-256 is
  `a22aa6d01c5245418c9eb27ec2e4a251dee24d891b2321e8fee8a8caea6b4d5d`; the
  byte-identical result hash is
  `4fa5fec6b131366e2edd2b9c4894e9bfcb17d0760d1ac494040ffe0abfdc9a22`. The
  pre-0008 failed result remains historical in
  `.open-mc-bootstrap/verification/evidence/cv32e40p-pmp-differential-pre-0008.json`.
- Seeded synchronous-fault differential: **12/12** across four seeds, varying
  the fault PC, misaligned address and signature for illegal instruction,
  misaligned word load and misaligned word store. Exact trap state and `mret`
  recovery match the independent reference in every case.
- Seeded PMP differential: **12/12** across four seeds for load, store and
  instruction denials, with exact trap state and denied-bus isolation.
- Seeded interrupt timing: **28/28** across four seeds and seven active
  standard machine-interrupt scenarios on the nine-patch profile.
- Bus/compressed faults: **36/36** across four seeds for delayed instruction,
  load and store responses, coincident data and prefetched-instruction errors,
  and four reserved compressed encodings. The simultaneous cases exposed a real
  exception-precision defect — a persistent prefetched fetch-fault level
  preempted the older in-flight data-fault trap and overwrote mcause/mtval —
  repaired by patch `0010-preserve-serviced-data-fault` (exception redirects
  discard the faulted prefetch; it is refetched from the handler return). The
  full campaign is re-run **36/36** on the revised ten-patch profile after the
  patch-0003 synthesis repair; the refreshed artifact binds all ten patch
  SHA-256 digests.
- Ten-patch physical refresh: the repaired profile maps to IHP SG13G2 through
  sv2v 0.0.13.1, Yosys 0.50 and OpenSTA 3.1.0 with the pinned provisional
  Liberty. The PMP-enabled mapping uses 25,421 cells and 325,905.8 um²; the
  screen meets 100 MHz (+2.3553 ns setup slack) and misses 150/200/250 MHz.
  On the official preview IHP PDK, the 100 MHz placement-aware screen passes
  (+0.6334 ns, TNS 0, placed area 390,982.5 um², estimated placed Fmax
  109.22 MHz) and the 150 MHz screen records an honest miss (-0.7374 ns, TNS
  -638 ns). Records:
  [mapping screen](evidence/open-mcu-cv32e40p-tenpatch-ihp-typical-screen.json),
  [100 MHz placement](evidence/open-mcu-cv32e40p-tenpatch-ihp-100mhz-placement.json),
  [150 MHz placement](evidence/open-mcu-cv32e40p-tenpatch-ihp-150mhz-placement.json).
- RVFI trace-integrity: 17 corpus instructions and 18 retire records, passed
  twice with the repaired eight-patch profile. Manifest
  `a715e4f6abef18b04fe6d905accfe28ddf942a329fedc7f6b6b1a2bc5c7f1c6a` and
  trace `bd35ee3f755ed34f0d397f93f151d189917f2b879604921bfae66c3a6e9ad0aa`.
  Refreshed twice on the revised ten-patch profile, byte-identical, with the
  same retire count and zero errors; the refreshed record is
  [ten-patch RVFI gate](evidence/open-mcu-cv32e40p-tenpatch-rvfi-integrity-gate.json).
- RVFI instruction-level reference comparison: 16/16 architectural retirements
  match twice with zero mismatches. Two post-halt records are explicitly ignored;
  15 next-PC transitions are covered. Manifest
  `d66f94dbce9091aac84a94376b6e6f0e8815154588acedf9b04dfdf45688282b`, trace
  `62561f51afd4b93aa2c9b6d45a541182eddd25897d6e63a750c1b27d1317ae78`, and
  reference model `9c191bb73399a082807f2731f303a0ff2bb318133a2fccb417bf8973316fc2af`.
  Memory-data comparison is unavailable because the upstream monitor leaves those
  fields empty.
- CSR/fence RVFI differential: 4 deterministic seeds × 32 generated operations,
  with 52/52 expected architectural retirements compared per case and 54
  observed records including two trailing post-halt records. All four cases
  passed twice. Manifest `e582da9dbad34ea878444e2d80f099bccc467b6791159c448e3bd69f903fcf3b`;
  the bounded lane covers `mscratch` (0x340), `fence`, and `fence.i` with MIE
  disabled. Counters/time and other CSRs remain excluded. Refreshed twice on
  the revised ten-patch profile, byte-identical:
  [ten-patch CSR/fence gate](evidence/open-mcu-cv32e40p-tenpatch-csr-fence-gate.json).
  The pinned RV32I legacy regression is also refreshed twice on the revised
  profile, byte-identical, with 44/48 passed and only the four documented
  upstream exclusions:
  [ten-patch RV32I regression](evidence/open-mcu-cv32e40p-tenpatch-rv32i-legacy-regression.json).
  The PMP access-fault and synchronous-fault differentials are refreshed
  byte-identically twice on the revised profile:
  [PMP](evidence/open-mcu-cv32e40p-tenpatch-pmp-differential-gate.json),
  [synchronous fault](evidence/open-mcu-cv32e40p-tenpatch-synchronous-fault-gate.json).

Historical superseded gate:

The prior synchronous-fault differential recorded **0/3** because the six-patch
profile returned `mtval=0` for illegal instruction and misaligned load/store
cases. Its independent expectations were `0xffffffff` and address `1`; the
historical artifact remains at
`930e30a54dc41216eed006dae3e0673fa3d43eb6f2aa7bcbc6199a4017a19dd9`.

Open gates:

1. Refresh the interrupt lifecycle/timing/nesting and fast-interrupt lanes on
   the revised patch-0003 profile (their committed evidence still binds the
   pre-repair patch SHA).
2. Reproduce ACT4 RV32IMC/Zicsr/Zifencei/PMP/InterruptsSm on the revised
   profile; this environment currently has no container runtime for the
   pinned ACT4 image.
3. Close routed timing at a met target: CTS, routing, hold, extraction,
   DRC/LVS and SDF post-layout simulation for the 100 MHz screen.
4. Select the final core and bind the product decision to reviewed evidence.

The evidence records are in [docs/evidence](evidence/). The product contract
and frequency strategy are in
[docs/open-mcu-milestone-definition.md](open-mcu-milestone-definition.md).

## Backlog

### Platform

- Add versioned project export/import with content-hash validation.
- Add the engineer CLI/TUI over REST, WebSocket jobs, logs, and evidence APIs.
- Finish durable cross-run token/cost accounting and clearer retry explanations.
- Extend the static Design Contract to expert constraints and assurance views.

### Verification and hardware

- Qualify generated-design regression, coverage, formal, equivalence, CDC/RDC,
  portability, and production lint as broader release gates.
- Select and purchase a board, then add the FPGA profile and hardware harness.
- Keep hardware evidence inconclusive until hardware is available.

### Manufacturing

- Complete provider-specific Tiny Tapeout submission and review.
- Select a shuttle or manufacturing route, then qualify its PDK, IO/wrapper,
  package, rule decks, cost, and release checks.
- Add independent review and release packaging before any production claim.

### Product and deployment

- Add authentication, ownership, origin controls, and resource limits before
  any network deployment.
- Add screenshots and short console workflows once the UI surface stabilizes.

## Assurance limits

- Open PDK synthesis, STA, power estimates, and coarse Fmax are screening or
  engineering evidence.
- A routed result with failed physical acceptance cannot support a closure
  claim. The Phase 3 audit remains the source of truth for that baseline.
- Tiny Tapeout hardening is an engineering step. Provider acceptance and foundry
  signoff remain separate open gates.
- Open_MC gates establish bounded architectural evidence for the repaired
  profile. Patches 0010 and the revised 0003 form the current repaired
  candidate RTL. Refreshed formal, differential and physical screening
  evidence binds the revised profile; routed physical closure, the
  interrupt-lane refresh and the ACT4 reproduction remain open. The remaining
  assurance scope includes complete ISA certification, silicon behavior,
  manufacturing readiness, or final core selection.
- Commercial/foundry signoff requires an approved PDK, libraries, rule decks,
  signoff flow, and independent review.

## Qualification references

- [Verification and evidence contract](agent-reference/verification-and-evidence.md)
- [EDA and operations](agent-reference/eda-and-operations.md)
- [Matmul qualification audit](s8-matmul-qualification-audit.md)
- [Tiny Tapeout guide](tapeout.md)
- [Phase 3 physical gap audit](phase3-physical-gap-audit.md)
