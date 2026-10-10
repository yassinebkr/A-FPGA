# Open MCU milestone definition

Status: DISCUSSION — architecture decisions are intentionally open.

## Position in the project

The open microcontroller is a milestone for qualifying the autonomous FPGA/ASIC
design platform across a complete, realistic silicon lifecycle. It exercises
multi-file architecture, independent verification, firmware, synthesis,
physical design, manufacturing preparation, packaged-silicon bring-up and
revision management.

This repository remains the platform repository. The microcontroller is
expected to become a separate derived project once its architecture and
evidence are sufficiently mature. That future repository will own the MCU's
product identity, releases, governance, documentation, firmware distribution
and silicon revisions. Until that split is approved, MCU work stays clearly
identified as milestone evidence inside this repository.

The frequency objectives are 250 MHz as the primary target and 500 MHz as a
technology-dependent stretch target. Neither is a tape-out claim until backed
by the selected PDK, libraries, memories, corners, constraints and post-route
evidence.

## Reference position

Croc is the pinned known-silicon reference and regression control described in
[the Croc audit](croc-reference-audit.md). Its hierarchy, wrappers,
reproducible manifests and open physical flow inform the milestone. The MCU
architecture and verification ownership remain independent.

## Decision sequence

Each point is discussed and recorded before the next point is frozen. Discussion is reserved for choices with material consequences for performance, area, verification, manufacturability, software compatibility or future evolution. Conventional choices with no meaningful project-specific tradeoff are recorded with a short rationale. No product RTL generation starts during this definition sequence.

1. **ISA and software contract — DECIDED**
   Baseline: RV32IMC with Zicsr and Zifencei, one core, Machine mode,
   little-endian and the ilp32 ABI. Misaligned accesses raise deterministic
   exceptions. The initial profile excludes A, F, D and V. Software targets
   are bare-metal, newlib, FreeRTOS and Zephyr. Multiplication and division
   implementation details remain a Point 2 architecture decision. Linux,
   Supervisor mode and an MMU belong to the future application-class milestone.

2. **Core and pipeline architecture — DIRECTION AGREED**
   Use a four-stage in-order pipeline with a 32 x 32-bit register file, a
   pipelined multiplier and an iterative divider. CV32E40P is the primary
   reference candidate for the first prototype; CVE2/Croc remains the
   known-silicon functional and physical control. Final core selection remains
   gated by a reproducible technology comparison covering timing, area, power,
   verification maturity and modification boundaries. The architecture must
   preserve a clean core wrapper so the platform and later derived MCU project
   can replace or evolve the core without rewriting the SoC.

3. **Execution, control and safety behavior**
   Freeze reset, traps, interrupts, debug, counters, protection, boot modes
   and deterministic fault behavior.

4. **Interconnect and extension contract**
   Select the internal bus, arbitration rules, address-map ownership,
   peripheral protocol and accelerator/user-domain boundary.

5. **Memory system and boot chain**
   Size ROM and SRAM from software needs and available technology macros;
   define initialization, firmware loading, execute-in-place expectations and
   technology-independent wrappers.

6. **Peripheral, IO and package baseline**
   Candidate first-prototype scope: UART, SPI master, I2C master, GPIO and a
   timer. I2C multi-master/slave modes and DMA are deferred candidates. Freeze
   exact capabilities later and derive clocks, resets, open-drain requirements,
   pads, pin budget, package assumptions, test access and board-level needs.

7. **Technology and frequency feasibility matrix**
   Evaluate the selected architecture at 100, 150, 200 and 250 MHz across
   declared corners and physical assumptions. Treat 500 MHz as a separate
   profile requiring its own technology and architecture decision.

8. **Verification, release and repository split gates**
   Define ISA compliance, independent references, coverage, formal, CDC/RDC,
   equivalence, DRC/LVS, post-layout and bring-up evidence. Define the exact
   maturity threshold and provenance package that permits creating the derived
   MCU repository.

## Phase exit

This definition phase closes when all eight decisions are reviewed, their
tradeoffs and open risks are durable, a core/SoC architecture is selected, and
the 250 MHz objective has a measured technology plan. The next phase can then
implement a bounded MCU baseline within the platform while preserving a clean
future extraction boundary.
