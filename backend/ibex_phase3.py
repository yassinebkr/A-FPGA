"""Reviewed Phase 3 physical constraint contract for the pinned Ibex core."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from constraints import (
    CaseAnalysisConstraint,
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

SCHEMA = "ibex-phase3-constraints/v1"
IBEX_COMMIT = "654ac71f91f7da5dc71942c9ba8d3103666c484e"
SOURCE_SHA256 = "068c27a0531207d003179ad76afed93720007d9dcc25c81a2b7710af4161e6be"
CLOCK_PORT = "clk_i"
RESET_PORT = "rst_ni"

INPUT_PORTS = (
    "hart_id_i[*]", "boot_addr_i[*]", "cheriot_enable_i[*]",
    "instr_gnt_i", "instr_rvalid_i", "instr_rdata_i[*]", "instr_err_i",
    "data_gnt_i", "data_rvalid_i", "data_rdata_i[*]", "data_tag_i",
    "data_err_i", "rf_rdata_a_ecc_i[*]", "rf_rdata_b_ecc_i[*]",
    "rf_rcap_a_ecc_i[*]", "rf_rcap_b_ecc_i[*]", "ic_tag_rdata_i[*]",
    "ic_data_rdata_i[*]", "ic_scr_key_valid_i", "irq_software_i",
    "irq_timer_i", "irq_external_i", "irq_fast_i[*]", "irq_nm_i",
    "debug_req_i", "fetch_enable_i[*]", "mcounteren_writable_i[*]",
)

OUTPUT_PORTS = (
    "instr_req_o", "instr_addr_o[*]", "data_req_o", "data_we_o",
    "data_be_o[*]", "data_addr_o[*]", "data_wdata_o[*]", "data_tag_o",
    "dummy_instr_id_o", "dummy_instr_wb_o", "rf_raddr_a_o[*]",
    "rf_raddr_b_o[*]", "rf_waddr_wb_o[*]", "rf_we_wb_o",
    "rf_wdata_wb_ecc_o[*]", "rf_wcap_ecc_wb_o[*]", "ic_tag_req_o[*]",
    "ic_tag_write_o", "ic_tag_addr_o[*]", "ic_tag_wdata_o[*]",
    "ic_data_req_o[*]", "ic_data_write_o", "ic_data_addr_o[*]",
    "ic_data_wdata_o[*]", "ic_scr_key_req_o", "irq_pending_o",
    "crash_dump_o[*]", "double_fault_seen_o", "alert_minor_o",
    "alert_major_internal_o", "alert_major_bus_o", "core_busy_o[*]",
)


def reviewed_constraints() -> ConstraintSet:
    """Return the exact conservative 50 MHz screening contract.

    The clock and asynchronous reset are deliberately excluded from the
    functional I/O delay/drive collections. The target metadata records the
    demo-PDK limitation; the adapter does not claim foundry signoff.
    """
    clock = "clk_i"
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
        input_drives=[
            InputDriveConstraint(
                port=port, library_cell="sky130_fd_sc_hd__buf_4")
            for port in INPUT_PORTS
        ],
        output_loads=[
            OutputLoadConstraint(port=port, capacitance_pf=0.03)
            for port in OUTPUT_PORTS
        ],
        case_analysis=[CaseAnalysisConstraint(
            port="cheriot_enable_i[*]", value=0,
            reason=("BaseIsa is fixed to RV32I in this artifact; CHERIoT is "
                    "not part of the qualified hardware profile."),
        )],
        electrical_limits=ElectricalLimits(
            max_transition_ns=1.5,
            max_capacitance_pf=0.2,
            max_fanout=32.0,
        ),
        false_paths=[FalsePathConstraint(
            **{"from": "rst_ni"},
            reason="asynchronous active-low reset; recovery/removal requires a dedicated check",
        )],
        assumptions=[
            "50 MHz screening target with a 20 ns primary clock.",
            "Functional inputs and outputs use 20 percent of the period as maximum external delay.",
            "Skywater demo slow-corner metadata is screening evidence, not foundry signoff.",
            "No multicycle timing exception is assumed.",
            "The converted artifact fixes BaseIsa=RV32I; cheriot_enable_i is case-analyzed Off. This is a profile assumption, not a false path.",
        ],
    )


class IbexConstraintAuditError(ValueError):
    """The reviewed constraint contract does not match the pinned artifact."""


def _base_port(selector: str) -> str:
    return selector.removesuffix("[*]")


def _converted_top_ports(verilog: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Read the non-ANSI port declarations emitted by the pinned sv2v flow."""
    match = re.search(r"\bmodule\s+ibex_core\s*\((.*?)\)\s*;", verilog, re.S)
    if match is None:
        raise IbexConstraintAuditError("ibex_core module declaration not found")
    declared = tuple(item.strip() for item in match.group(1).split(",") if item.strip())
    body = verilog[match.end():]
    body = body[:body.find("endmodule")]
    directions: dict[str, str] = {}
    for direction, name in re.findall(
        r"^\s*(input|output)\s+(?:(?:wire|reg|logic)\s+)?"
        r"(?:\[[^;]+?\]\s+)?([A-Za-z_][A-Za-z0-9_$]*)\s*;",
        body,
        re.M,
    ):
        if name in declared:
            directions[name] = direction
    missing = sorted(set(declared) - directions.keys())
    if missing:
        raise IbexConstraintAuditError(
            "top-level port directions missing: " + ", ".join(missing))
    inputs = tuple(port for port in declared if directions[port] == "input")
    outputs = tuple(port for port in declared if directions[port] == "output")
    return inputs, outputs


def audit_converted_artifact(verilog: str, *, require_pinned_digest: bool = True) -> dict:
    """Fail closed unless every real top port has one reviewed disposition."""
    digest = hashlib.sha256(verilog.encode("utf-8")).hexdigest()
    if require_pinned_digest and digest != SOURCE_SHA256:
        raise IbexConstraintAuditError(
            f"converted artifact digest mismatch: expected {SOURCE_SHA256}, got {digest}")
    inputs, outputs = _converted_top_ports(verilog)
    expected_inputs = {_base_port(port) for port in INPUT_PORTS}
    expected_outputs = {_base_port(port) for port in OUTPUT_PORTS}
    actual_functional_inputs = set(inputs) - {CLOCK_PORT, RESET_PORT}
    problems = {
        "missing_input_constraints": sorted(actual_functional_inputs - expected_inputs),
        "stale_input_constraints": sorted(expected_inputs - actual_functional_inputs),
        "missing_output_constraints": sorted(set(outputs) - expected_outputs),
        "stale_output_constraints": sorted(expected_outputs - set(outputs)),
    }
    if any(problems.values()):
        detail = "; ".join(f"{key}={value}" for key, value in problems.items() if value)
        raise IbexConstraintAuditError(f"Ibex port coverage mismatch: {detail}")
    if CLOCK_PORT not in inputs or RESET_PORT not in inputs:
        raise IbexConstraintAuditError("clock/reset ports are absent from the pinned top")
    return {
        "schema": "ibex-phase3-port-audit/v1",
        "artifact_sha256": digest,
        "top_module": "ibex_core",
        "port_count": len(inputs) + len(outputs),
        "functional_input_count": len(actual_functional_inputs),
        "output_count": len(outputs),
        "clock": {"port": CLOCK_PORT, "disposition": "primary_clock"},
        "reset": {
            "port": RESET_PORT,
            "disposition": "asynchronous_reset_false_path",
            "limitation": "recovery/removal requires a dedicated check",
        },
        "coverage": "complete",
    }


def write_constraint_evidence(
    destination: str | Path,
    *,
    converted_artifact: str | Path | None = None,
) -> dict:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    constraints = reviewed_constraints()
    sdc = constraints.to_sdc()
    (destination / "ibex_core.sdc").write_text(sdc, encoding="utf-8")
    payload = {
        "schema": SCHEMA,
        "ibex_commit": IBEX_COMMIT,
        "source_sha256": SOURCE_SHA256,
        "constraint_sha256": constraints.content_sha256(),
        "constraints": constraints.canonical_data(),
        "sdc_sha256": hashlib.sha256(sdc.encode()).hexdigest(),
        "adapter_limitations": [
            "Target corner, voltage, and temperature are metadata; SiliconCompiler target selection binds libraries.",
            "Asynchronous reset recovery/removal is not proven by the false-path constraint.",
        ],
    }
    if converted_artifact is not None:
        artifact_path = Path(converted_artifact)
        payload["port_audit"] = audit_converted_artifact(
            artifact_path.read_text(encoding="utf-8"))
    (destination / "constraints.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


__all__ = ["reviewed_constraints", "write_constraint_evidence",
           "audit_converted_artifact", "IbexConstraintAuditError", "INPUT_PORTS",
           "OUTPUT_PORTS", "IBEX_COMMIT", "SOURCE_SHA256", "CLOCK_PORT",
           "RESET_PORT"]
