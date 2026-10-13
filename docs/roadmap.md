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

## Open_MC active gates

Passed gates for the repaired CV32E40P profile:

- ACT4 RV32IMC, Zicsr, and Zifencei: 80/80, reproduced byte-identically.
- Privileged PMP: 45/45 twice. PMPSm: 33/33. PMPZca: 12/12.
- Interrupt lifecycle harness: 7/7 twice. It covers machine-mode entry,
  masking, mret, software/timer/external acknowledgement at 0x20000004,
  and mcause/mepc checks.
- Sail differential: 16 deterministic seeds × 128 operations against Sail
  0.13.1 from the pinned ACT4 image.

Open gates:

1. Compare the RVFI retire trace against the reference.
2. Extend randomized differential coverage to CSR, fence, interrupt, PMP, and
   fault behavior.
3. Complete ACT4 InterruptsSm certification.
4. Refresh physical evidence with the selected configuration and technology.
5. Select the final core and bind the product decision to reviewed evidence.

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
  profile. The remaining assurance scope includes complete ISA certification, silicon behavior,
  manufacturing readiness, or final core selection.
- Commercial/foundry signoff requires an approved PDK, libraries, rule decks,
  signoff flow, and independent review.

## Qualification references

- [Verification and evidence contract](agent-reference/verification-and-evidence.md)
- [EDA and operations](agent-reference/eda-and-operations.md)
- [Matmul qualification audit](s8-matmul-qualification-audit.md)
- [Tiny Tapeout guide](tapeout.md)
- [Phase 3 physical gap audit](phase3-physical-gap-audit.md)
