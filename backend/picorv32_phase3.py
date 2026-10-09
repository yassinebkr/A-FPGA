"""Reviewed 50 MHz physical constraint contract for the pinned PicoRV32 control."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from constraints import (
    ClockConstraint,
    ClockUncertaintyConstraint,
    ConstraintSet,
    ElectricalLimits,
    FalsePathConstraint,
    InputDriveConstraint,
    IoDelayConstraint,
    OutputLoadConstraint,
    TargetConstraint,
)

SCHEMA = "picorv32-phase3-constraints/v1"
PICORV32_COMMIT = "ef203c2b0a3fb793280f5114941416c425c5b461"
MANIFEST_DIGEST = "52643fb6dd7f1cb5ed44425b2adbe06fd5e7e48ced4a2a1ffeb4cdee2e42e2bb"

INPUT_PORTS = (
    "mem_ready", "mem_rdata[*]", "pcpi_wr", "pcpi_rd[*]", "pcpi_wait",
    "pcpi_ready", "irq[*]",
)
OUTPUT_PORTS = (
    "trap", "mem_valid", "mem_instr", "mem_addr[*]", "mem_wdata[*]",
    "mem_wstrb[*]", "mem_la_read", "mem_la_write", "mem_la_addr[*]",
    "mem_la_wdata[*]", "mem_la_wstrb[*]", "pcpi_valid", "pcpi_insn[*]",
    "pcpi_rs1[*]", "pcpi_rs2[*]", "eoi[*]", "trace_valid", "trace_data[*]",
)


def reviewed_constraints() -> ConstraintSet:
    clock = "clk"
    return ConstraintSet(
        target=TargetConstraint(
            process="sky130", device="skywater130_demo",
            library="sky130_fd_sc_hd", corner="slow",
            voltage_v=1.4, temperature_c=-40.0,
        ),
        clocks=[ClockConstraint(name=clock, port=clock, period_ns=20.0)],
        uncertainties=[ClockUncertaintyConstraint(
            clock=clock, setup_ns=0.25, hold_ns=0.25)],
        io_delays=[
            *(IoDelayConstraint(port=port, direction="input", clock=clock,
                                min_delay_ns=0.0, max_delay_ns=4.0)
              for port in INPUT_PORTS),
            *(IoDelayConstraint(port=port, direction="output", clock=clock,
                                min_delay_ns=0.0, max_delay_ns=4.0)
              for port in OUTPUT_PORTS),
        ],
        input_drives=[InputDriveConstraint(
            port=port, library_cell="sky130_fd_sc_hd__buf_4")
            for port in INPUT_PORTS],
        output_loads=[OutputLoadConstraint(port=port, capacitance_pf=0.03)
                      for port in OUTPUT_PORTS],
        electrical_limits=ElectricalLimits(
            max_transition_ns=1.5, max_capacitance_pf=0.2, max_fanout=32.0),
        false_paths=[FalsePathConstraint(
            **{"from": "resetn"},
            reason=("asynchronous active-low reset; recovery/removal requires "
                    "a dedicated check"),
        )],
        assumptions=[
            "50 MHz screening target with a 20 ns primary clock.",
            "Functional I/O uses 4 ns maximum external delay.",
            "No multicycle timing exception is assumed.",
            "Skywater demo slow-corner metadata is screening evidence, not signoff.",
        ],
    )


def write_constraint_evidence(destination: str | Path) -> dict:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    constraints = reviewed_constraints()
    sdc = constraints.to_sdc()
    sdc_path = destination / "picorv32.sdc"
    sdc_path.write_text(sdc, encoding="utf-8")
    payload = {
        "schema": SCHEMA,
        "source_commit": PICORV32_COMMIT,
        "manifest_digest": MANIFEST_DIGEST,
        "constraint_sha256": constraints.content_sha256(),
        "constraints": constraints.canonical_data(),
        "sdc_sha256": hashlib.sha256(sdc.encode()).hexdigest(),
        "input_ports": list(INPUT_PORTS),
        "output_ports": list(OUTPUT_PORTS),
    }
    (destination / "constraints.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


__all__ = ["reviewed_constraints", "write_constraint_evidence", "INPUT_PORTS",
           "OUTPUT_PORTS", "PICORV32_COMMIT", "MANIFEST_DIGEST"]
