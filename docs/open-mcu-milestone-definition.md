# Open MCU milestone definition

Status: CONTRACT LOCKED — implementation qualification remains pending.

## Position in the project

The open microcontroller is a milestone for qualifying the autonomous FPGA/ASIC
design platform across a complete, realistic silicon lifecycle. It exercises
multi-file architecture, independent verification, firmware, synthesis,
physical design, manufacturing preparation, packaged-silicon bring-up and
revision management.

This repository remains the platform repository. The microcontroller now has a dedicated private product repository at https://github.com/yassinebkr/Open_MC. Product RTL, firmware, verification, physical manifests, board material and silicon releases belong there from the first implementation revision. This platform repository owns orchestration, reusable EDA and assurance infrastructure, and qualification evidence bound to exact Open_MC commits. Public release remains gated by provenance, verification and silicon-release criteria.

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
   Use a four-stage in-order pipeline with a 32 × 32-bit register file, a
   pipelined multiplier and an iterative divider. Keep a clean core wrapper so
   the platform and derived MCU can evolve independently.

   Measured status:

   - CV32E40P is the implementation candidate; CVE2/Croc is the control.
   - Both close 100 MHz and miss 150 MHz in the matched IHP placement screen.
   - Estimated placed Fmax: CV32E40P 127.0519 MHz; CVE2 123.0456 MHz.
   - Directed repaired-profile corpus: 15/15.
   - Legacy RV32I suite: 44/48, with four declared exclusions.
   - ACT4 base product profile: 80/80, reproduced byte-identically.
   - ACT4 privileged PMP: 38/45; the gate remains open.

   The fourth repair completes denied instruction fetch responses and closes two
   aligned compressed-access cases. Twelve failures remain across permissions,
   address modes, priority and compressed boundaries. Final selection still
   requires PMP repair, interrupt-harness coverage, randomized differential
   verification and refreshed physical evidence.

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

8. **Verification, release and repository split gates — DECIDED**
   Develop product material in the dedicated private Open_MC repository and
   bind every platform result to exact A-FPGA and Open_MC commits. Gate imported
   material on license and provenance records. Gate the core on RISC-V
   compliance, independent differential references, reproducible random tests,
   formal properties and reviewed functional coverage. Gate the SoC on boot,
   memory, bus-error, interrupt, debug and peripheral firmware tests. Require a
   versioned lint policy, CDC/RDC, assertions, justified coverage exclusions,
   reproducible synthesis and RTL-to-netlist equivalence. Gate physical releases
   on the Point 7 criteria and freeze GDS, netlists, constraints, reports,
   firmware, board data, tools, licenses and hashes. Complete silicon bring-up
   and characterization before a production claim. Publication of Open_MC and
   each manufactured release requires all applicable gates; repository creation
   and early private development are authorized immediately.

## Phase exit

The definition contract is locked after review of all eight decisions. The
architecture direction and 250 MHz measurement plan are durable; final core
selection remains gated by the reproducible technology comparison in Point 2.
Implementation now begins as a bounded baseline in Open_MC, while A-FPGA
qualifies each exact product revision.

The product repository owns the locked licensing and provenance policy. A-FPGA
must reject qualification evidence whose Open_MC revision lacks a reviewed,
immutable dependency manifest for imported material.

## Learning track

The French-language [learning path](../learning/README.md) explains each frozen decision for readers new to processor and silicon design. It is non-normative and grows after each decision is locked; this contract remains the engineering source of truth.
