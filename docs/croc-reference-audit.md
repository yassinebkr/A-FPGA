# Croc reference audit for the open-microcontroller milestone

Status: COMPLETE — read-only upstream audit, 2026-10-10.

## Scope and provenance

This audit evaluates Croc as a reference for the active open, modifiable,
manufacturable microcontroller milestone. No Croc source was imported into this
repository and no local EDA run was launched.

- Upstream: <https://github.com/pulp-platform/croc>
- Audited main revision: `b301ef4439ab9b55322d2ac31d672fdcc67bc0c6`
- Frozen MLEM tape-out revision:
  `f54fdab3f463181f6b91646e36b29321c39432b1`
- Main revision inventory: 189 SystemVerilog files, 206 declared modules and
  26 C/header/assembly software files.
- The repository states that MLEM taped out in November 2024 and that its core
  functionality was verified on real silicon in early 2026.

The current main branch has successful public Short Flow and Full Flow GitHub
Actions runs for the audited revision. The Full Flow publishes OpenROAD, GDS
and minimal-DRC artifacts. GitHub requires authentication to download those
artifacts, so this audit does not independently reproduce their report values.

## Architecture

Croc is a compact RV32 microcontroller-class SoC organized around:

- a two-stage CVE2/CV32E20 core derived from Ibex;
- separate OBI instruction and data master ports;
- an OBI crossbar and peripheral demultiplexer;
- technology-wrapped banked SRAM;
- boot ROM, CLINT, SoC control, UART, GPIO and timer;
- RISC-V debug through JTAG;
- an optional iDMA configuration;
- a user domain for extensions and accelerators;
- chip-level IO, bond-pad and physical wrappers.

The audited default configuration uses a full 32-register core, disables the
M and B extensions, disables PMP and instantiates two 512-word, 32-bit SRAM
banks. Boot currently depends on JTAG loading. The configuration and address
map are centralized in `rtl/croc_pkg.sv`; the core boundary is isolated in
`rtl/core_wrap.sv`.

This decomposition is a strong reference for our project manifest, hierarchy,
source navigation, technology wrappers and modification workflow.

## Reproducibility and licensing

Bender defines source order, targets and dependencies. `Bender.lock` resolves
the dependency graph, while several dependencies are vendored into `rtl/`
with recorded patches. PDK mirrors include origins, pinned revisions, licenses
and file digests.

Hardware and tool scripts are generally under Solderpad Hardware License 0.51.
Software is generally under Apache-2.0. Individual vendored files retain their
own headers. Any reuse requires a machine-readable bill of materials and
per-file license/provenance preservation.

The main branch pins IHP SG13G2 and SG13CMOS5L submodules and defaults its ASIC
flow to SG13CMOS5L. SRAM selection is technology-aware and unsupported
configurations deliberately instantiate an identifiable black box.

## Verification evidence

The current CI performs:

- Verilator RTL simulation;
- a hello-world and configuration test;
- nine software-directed tests covering compute, CLINT, GPIO, iDMA,
  interrupts, timer, SoC control and UART;
- Yosys synthesis for default and iDMA configurations;
- post-Yosys netlist simulation;
- OpenROAD placement and routing;
- GDS generation, sealing and KLayout minimal DRC;
- formatting and selected source/license checks.

The repository explicitly states that the complete CVE2 verification
environment lives elsewhere. The audited Croc repository does not expose an
integrated release gate for ISA compliance, functional or structural coverage,
formal properties, RTL-to-netlist equivalence, automated CDC/RDC analysis, LVS,
full DRC, post-layout simulation or silicon characterization results. The SDC
does explicitly identify the system, JTAG and reference clocks as asynchronous
and constrains known JTAG CDC structures.

Our MCU release gate must add these missing assurance layers and bind them to
the exact project, tool, PDK, constraint and firmware digests.

## Physical flow and frequency evidence

Croc supplies open Yosys, OpenROAD and KLayout flows, chip-level IO, SRAM
macros, power-grid scripts, pad placement, GDS generation and minimal/full DRC
modes.

- The frozen MLEM tape-out branch specifies an 80 MHz system clock
  (12.5 ns), 50 MHz JTAG and 20 MHz reference clock.
- The audited main branch specifies a 100 MHz system clock (10 ns), 40 MHz JTAG
  and 20 MHz reference clock.
- The main synthesis script maps combinational blocks with a 10 ns ABC target.
- The checked-in repository contains flow definitions and CI artifact links;
  it does not contain a durable report proving post-route slack or measured
  silicon Fmax for the audited revision.
- Minimal DRC is the automated CI check. Croc documents full DRC as the
  pre-tape-out manual mode.

Croc therefore proves that an end-to-end open educational MCU can reach real
IHP silicon. It does not establish 250 or 500 MHz for our product.

## Adoption decision

Use Croc as a pinned external reference and regression fixture. Preserve these
ideas in our own architecture:

1. a narrow, explicit core wrapper;
2. typed OBI-like instruction/data and peripheral interfaces;
3. centralized address-map and product configuration;
4. generic and technology-specific SRAM wrappers;
5. a user extension domain;
6. reproducible RTL, netlist and physical views;
7. versioned dependency, patch, license and PDK manifests;
8. software-directed RTL and mapped-netlist tests.

Do not copy Croc wholesale into the active product. Our MCU needs a distinct
product contract, independent verification ownership and a timing architecture
designed around the 250 MHz goal. Croc remains the known-good silicon control.

## Decisions required before RTL

1. Select the exact ISA profile: proposed baseline RV32IMC plus Zicsr and
   Zifencei, subject to verification and compiler review.
2. Choose or design the pipeline and register-file architecture from measured
   technology data.
3. Freeze debug, interrupt, exception, boot and privilege behavior.
4. Select the on-chip bus and define extension/peripheral contracts.
5. Determine ROM/SRAM sizes from available IHP macros and their timing models.
6. Define the minimum product peripherals and package/IO budget.
7. Run a technology feasibility study at 100, 150, 200 and 250 MHz before
   making 250 MHz a tape-out acceptance criterion.
8. Treat 500 MHz as a separate stretch profile and require a technology
   decision after the 250 MHz study.

## Immediate next phase

Create the MCU product contract and technology feasibility matrix. Compare at
least CVE2, Ibex and CV32E40P-class pipelines against our desired ISA,
performance, area, verification maturity and modification boundaries. Use
Croc's exact 80/100 MHz configurations as controls. The phase ends with a
reviewed architecture selection and a measured plan for 250 MHz; it does not
start product RTL generation.
