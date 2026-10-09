import hashlib
import json
from pathlib import Path

import pytest

from physical_gate import PhysicalGateError, evaluate_physical_gate


def _file(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(tmp_path: Path, **overrides):
    job = tmp_path / "job0"
    reports = job / "reports"
    paths = {
        "drv_violators": _file(reports / "drv.rpt"),
        "floating_nets": _file(reports / "floating.rpt"),
        "overdriven_nets": _file(reports / "overdriven.rpt"),
        "unconstrained_endpoints": _file(reports / "unconstrained.rpt"),
        "setup_slack": _file(reports / "setup.rpt", "worst slack max 0.10\n"),
        "hold_slack": _file(reports / "hold.rpt", "worst slack min 0.05\n"),
        "high_fanout": _file(reports / "fanout.rpt", "net fanout\nn1 64\n"),
        "drc": _file(reports / "drc.rpt"),
        "antenna": _file(reports / "antenna.rpt"),
    }
    for name, content in overrides.items():
        paths[name] = _file(reports / f"{name}.override.rpt", content)
    artifact = _file(tmp_path / "top.v", "module top; endmodule\n")
    reviewed = _file(tmp_path / "reviewed.sdc", "create_clock -period 20 clk\n")
    consumed = _file(job / "consumed.sdc", reviewed.read_text())
    manifest = _file(job / "top.pkg.json", json.dumps({"design": "top"}))
    netlist = _file(job / "top.vg", "module top; endmodule\n")
    record = {
        "pnr_completed": True,
        "manifest_sha256": _sha(manifest),
        "source_artifact_sha256": _sha(artifact),
        "tool_versions": {"openroad": "test"},
        "target": "skywater130_demo",
        "detailed_route_violations": 0,
        "remaining_antenna_violations": 0,
    }
    record_path = _file(tmp_path / "run.json", json.dumps(record))
    return paths, artifact, reviewed, consumed, manifest, netlist, record_path


def _evaluate(tmp_path: Path, *, overrides=None, physical_only_endpoints=(),
              mutate=None):
    values = list(_inputs(tmp_path, **(overrides or {})))
    if mutate:
        mutate(*values)
    reports, artifact, reviewed, consumed, manifest, netlist, record = values
    return evaluate_physical_gate(
        reports, artifact_path=artifact, reviewed_sdc_path=reviewed,
        consumed_sdc_path=consumed, manifest_path=manifest,
        run_record_path=record, physical_netlist_path=netlist,
        toolchain={"openroad": "test"},
        target={"target": "skywater130_demo", "pdk": "sky130", "corner": "slow"},
        physical_only_endpoints=physical_only_endpoints)


def test_clean_gate_passes_and_high_fanout_is_diagnostic(tmp_path):
    result = _evaluate(tmp_path)
    assert result["status"] == "pass"
    assert result["metrics"]["maximum_fanout"] == 64
    assert result["blockers"] == []
    assert len(result["gate_digest"]) == 64


@pytest.mark.parametrize(("name", "content", "blocker"), [
    ("setup_slack", "worst slack max -1.25\n", "setup"),
    ("hold_slack", "worst slack min -0.01\n", "hold"),
    ("drv_violators", """max capacitance
Pin Limit Cap Slack
-------------------
u1/A 0.2 0.3 -0.1 (VIOLATED)
""", "max_capacitance"),
    ("floating_nets", "floating_a\n", "floating_nets"),
    ("overdriven_nets", "multi_driver\n", "overdriven_nets"),
    ("unconstrained_endpoints", "u_logic/A\n", "unconstrained_functional_endpoints"),
    ("drc", "metal spacing violation\n", "drc"),
    ("antenna", "antenna ratio violation\n", "antenna"),
])
def test_each_policy_blocker_fails(tmp_path, name, content, blocker):
    result = _evaluate(tmp_path, overrides={name: content})
    assert result["status"] == "fail"
    assert blocker in result["blockers"]


def test_constraint_mismatch_fails(tmp_path):
    def mutate(reports, artifact, reviewed, consumed, manifest, netlist, record):
        consumed.write_text("create_clock -period 10 clk\n")
    result = _evaluate(tmp_path, mutate=mutate)
    assert "constraint_mismatch" in result["blockers"]


def test_only_reported_and_netlist_proven_diode_can_be_classified(tmp_path):
    unconstrained = """Startpoint: irq_i (input port)
Endpoint: ANTENNA_24/DIODE (internal pin)
Path Group: unconstrained
Path Type: max
"""
    def add_diode(reports, artifact, reviewed, consumed, manifest, netlist, record):
        netlist.write_text(
            "module top;\nsky130_fd_sc_hd__diode_2 ANTENNA_24 (.DIODE(n));\nendmodule\n")
    result = _evaluate(
        tmp_path, overrides={"unconstrained_endpoints": unconstrained},
        physical_only_endpoints=["ANTENNA_24/DIODE"], mutate=add_diode)
    assert result["status"] == "pass"
    with pytest.raises(PhysicalGateError, match="no matching diode"):
        _evaluate(
            tmp_path, overrides={"unconstrained_endpoints": unconstrained},
            physical_only_endpoints=["ANTENNA_24/DIODE"])


def test_manifest_and_run_record_mismatch_fail(tmp_path):
    def bad_manifest(reports, artifact, reviewed, consumed, manifest, netlist, record):
        manifest.write_text('{"tampered": true}')
    with pytest.raises(PhysicalGateError, match="manifest digest"):
        _evaluate(tmp_path, mutate=bad_manifest)

    def bad_tool(reports, artifact, reviewed, consumed, manifest, netlist, record):
        payload = json.loads(record.read_text())
        payload["tool_versions"] = {"openroad": "other"}
        record.write_text(json.dumps(payload))
    with pytest.raises(PhysicalGateError, match="toolchain"):
        _evaluate(tmp_path / "tool", mutate=bad_tool)


def test_empty_checks_require_completed_zero_count_evidence(tmp_path):
    def skipped(reports, artifact, reviewed, consumed, manifest, netlist, record):
        payload = json.loads(record.read_text())
        payload["detailed_route_violations"] = None
        record.write_text(json.dumps(payload))
    with pytest.raises(PhysicalGateError, match="lacks manifest-proven"):
        _evaluate(tmp_path, mutate=skipped)


def test_missing_malformed_and_foreign_reports_fail_closed(tmp_path):
    values = list(_inputs(tmp_path))
    reports, artifact, reviewed, consumed, manifest, netlist, record = values
    reports.pop("drc")
    with pytest.raises(PhysicalGateError, match="missing required"):
        evaluate_physical_gate(
            reports, artifact_path=artifact, reviewed_sdc_path=reviewed,
            consumed_sdc_path=consumed, manifest_path=manifest,
            run_record_path=record, physical_netlist_path=netlist,
            toolchain={"openroad": "test"},
            target={"target": "skywater130_demo"})

    with pytest.raises(PhysicalGateError, match="malformed max slack"):
        _evaluate(tmp_path / "bad", overrides={"setup_slack": "slack maybe bad\n"})

    def foreign(reports, artifact, reviewed, consumed, manifest, netlist, record):
        reports["hold_slack"] = _file(tmp_path / "foreign.rpt", "worst slack min 0.1\n")
    with pytest.raises(PhysicalGateError, match="outside the manifest job root"):
        _evaluate(tmp_path / "foreign-case", mutate=foreign)
