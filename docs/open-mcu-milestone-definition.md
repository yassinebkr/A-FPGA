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

3. **Execution, control and safety behavior — DECIDED**
   Boot autonomously through immutable ROM and external SPI flash, with
   standard RISC-V JTAG debug as the recovery and bring-up path. Support
   machine timer, software and peripheral interrupts in direct and vectored
   modes; expose 64-bit cycle and retired-instruction counters. Implement eight
   PMP regions, precise exceptions and deterministic reporting for illegal
   instructions, misaligned accesses and protection faults. Use one main clock
   domain, immediate reset assertion and synchronous reset release. General
   lockstep, pervasive ECC and safety redundancy are deferred; memory-specific
   integrity is decided with the memory system.

4. **Interconnect and extension contract — DECIDED**
   Use 32-bit OBI with separate instruction and data paths, hierarchical
   registered interconnect cuts and at most one outstanding transfer per master
   in the first prototype. Support byte, half-word and word accesses,
   deterministic bounded arbitration and explicit errors for invalid addresses
   or stalled targets. Map peripherals into memory and generate RTL and
   software constants from one centrally owned address map. Expose a stable
   extension slot with an OBI slave interface and interrupt. Reserve a future
   extension-master capability without activating it in the first silicon.

5. **Memory system and boot chain — DECIDED**
   Provide 8 KiB of immutable boot ROM and target 128 KiB of SRAM split into
   four banks, with a contract-compatible 64 KiB fallback if the selected
   technology macros make the larger profile physically unsuitable. Store
   firmware in external SPI flash, validate its versioned image and CRC32, copy
   it into SRAM and execute it there. Support variable-latency memory handshakes,
   JTAG and UART recovery, UART firmware loading and a deterministic
   startup SRAM test. Defer execute-in-place
   caching and generalized ECC; decide optional parity from the actual SRAM
   macro widths and timing models. Use technology-independent wrappers around
   every ROM and SRAM implementation.

6. **Peripheral, IO and package baseline — DECIDED**
   Provide a FIFO-backed 8N1 UART, SPI master with modes 0-3 and two chip
   selects, I2C master at 100/400 kHz with clock stretching, eight interrupt-
   capable GPIOs, one 32-bit general timer and a watchdog. Share the SPI bus
   between boot flash and one external target. Use an external clock and no
   mandatory PLL in the first silicon. Budget approximately 26 functional
   signals and target a provider-supported package compatible with a QFN48-
   class pin count. The development board must provide USB-C programming and
   console through a USB-to-UART bridge; ROM must support UART recovery and
   firmware loading, while JTAG remains available for debug. Native USB PHY and
   device logic, DMA, advanced PWM and complex I2C modes are later profiles.
   Derive ESD, open-drain, voltage-level, pad-ring and bonding details from the
   provider-approved IO libraries and package.

7. **Technology and frequency feasibility matrix — DECIDED**
   Keep a technology-independent simulation profile and use IHP SG13G2 as the
   primary physical candidate, subject to written provider confirmation of the
   exact PDK, standard-cell, SRAM, IO, package and shuttle revisions. Retain
   SG13CMOS5L as a comparison profile without manufacturing-target status.
   Measure the core, core-memory subsystem and complete chip successively at
   100, 150, 200 and 250 MHz over declared PVT corners. Accept the 250 MHz
   product objective only after clean post-route multi-corner timing, electrical
   rules, clock and power integrity, DRC, LVS, antenna and equivalence evidence;
   require silicon characterization for the final frequency claim. Any miss
   triggers an explicit architecture, memory or technology decision. Treat
   500 MHz as a separate future technology profile.

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

## Learning track

The French-language [learning path](../learning/README.md) explains each frozen decision for readers new to processor and silicon design. It is non-normative and grows after each decision is locked; this contract remains the engineering source of truth.
