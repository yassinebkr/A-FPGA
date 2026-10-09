import hashlib
import json

import pytest

from ibex_phase3 import (
    INPUT_PORTS,
    OUTPUT_PORTS,
    IbexConstraintAuditError,
    audit_converted_artifact,
    reviewed_constraints,
    write_constraint_evidence,
)


def _converted_fixture(*, extra_port: str = "") -> str:
    inputs = ["clk_i", "rst_ni", *(item.removesuffix("[*]") for item in INPUT_PORTS)]
    outputs = [item.removesuffix("[*]") for item in OUTPUT_PORTS]
    if extra_port:
        inputs.append(extra_port)
    ports = ",\n  ".join([*inputs, *outputs])
    declarations = "\n".join(
        [*(f"input wire {item};" for item in inputs),
         *(f"output wire {item};" for item in outputs)]
    )
    return f"module ibex_core (\n  {ports}\n);\n{declarations}\nendmodule\n"


def test_reviewed_ibex_constraints_are_explicit_and_exclude_clock_reset():
    constraints = reviewed_constraints()
    constrained_inputs = {
        item.port for item in constraints.io_delays if item.direction == "input"
    }
    constrained_outputs = {
        item.port for item in constraints.io_delays if item.direction == "output"
    }
    assert constrained_inputs == set(INPUT_PORTS)
    assert constrained_outputs == set(OUTPUT_PORTS)
    assert "clk_i" not in constrained_inputs
    assert "rst_ni" not in constrained_inputs
    assert len(constraints.clocks) == 1
    assert constraints.clocks[0].period_ns == 20.0
    assert constraints.target.corner == "slow"
    assert not constraints.multicycle_paths
    assert [(item.port, item.value) for item in constraints.case_analysis] == [("cheriot_enable_i[*]", 0)]


def test_reviewed_ibex_sdc_has_complete_electrical_contract():
    sdc = reviewed_constraints().to_sdc()
    assert "create_clock -name clk_i -period 20" in sdc
    assert "set_false_path -from [get_ports {rst_ni}]" in sdc
    assert "set_max_transition 1.5 [current_design]" in sdc
    assert "set_max_capacitance 0.2 [current_design]" in sdc
    assert "set_max_fanout 32 [current_design]" in sdc
    assert "[all_inputs]" not in sdc
    assert "[all_outputs]" not in sdc
    assert "set_case_analysis 0 [get_ports {cheriot_enable_i[*]}]" in sdc
    for port in (*INPUT_PORTS, *OUTPUT_PORTS):
        assert f"[get_ports {{{port}}}]" in sdc


def test_constraint_evidence_binds_json_and_sdc(tmp_path):
    payload = write_constraint_evidence(tmp_path)
    sdc = (tmp_path / "ibex_core.sdc").read_bytes()
    persisted = json.loads((tmp_path / "constraints.json").read_text())
    assert persisted == payload
    assert payload["sdc_sha256"] == hashlib.sha256(sdc).hexdigest()
    assert len(payload["constraint_sha256"]) == 64


def test_port_audit_covers_every_real_port_and_keeps_clock_reset_separate():
    report = audit_converted_artifact(
        _converted_fixture(), require_pinned_digest=False)
    assert report["coverage"] == "complete"
    assert report["port_count"] == 61
    assert report["functional_input_count"] == 27
    assert report["output_count"] == 32
    assert report["clock"]["disposition"] == "primary_clock"
    assert report["reset"]["disposition"] == "asynchronous_reset_false_path"


def test_port_audit_fails_closed_on_digest_or_interface_drift():
    with pytest.raises(IbexConstraintAuditError, match="digest mismatch"):
        audit_converted_artifact(_converted_fixture())
    with pytest.raises(IbexConstraintAuditError, match="missing_input_constraints"):
        audit_converted_artifact(
            _converted_fixture(extra_port="new_unreviewed_i"),
            require_pinned_digest=False,
        )
