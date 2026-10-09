import json
from pathlib import Path
from types import SimpleNamespace

from evidence import EvidenceRepository
from verification_executor import (
    FunctionalCoverageAdapter,
    VerilatorCapabilities,
    VerilatorCoverageExecutor,
    VerilatorAssertionExecutor,
    _parse_coverage_summary,
)


def test_functional_adapter_has_cocotb_style_bins_and_crosses():
    spec = {"design_class": "protocol", "verification": {
        "coverage_requirements": [
            {"id": "transfer", "target": "transfer", "threshold": .5},
            {"id": "ordering", "target": "ordering", "threshold": 1.0},
        ]}}
    adapter = FunctionalCoverageAdapter.from_design_spec(spec)
    adapter.add_point("transfer", ["idle", "active"])
    adapter.add_cross("ordering", ["valid", "ready"])
    adapter.sample("transfer", "idle")
    adapter.sample("ordering", event={"valid": 1, "ready": 0})
    result = {item.requirement_id: item for item in adapter.results()}
    assert result["transfer"].covered == 1 and result["transfer"].total == 2
    assert result["ordering"].covered == 1 and result["ordering"].total == 1


def test_summary_normalization_is_release_tolerant():
    report = _parse_coverage_summary("Line coverage: 80.00%\nToggle coverage 50%\nTotal 75.0%")
    assert report["line"]["percentage"] == .8
    assert report["toggle"]["percentage"] == .5
    assert report["overall_percent"] == .75


def test_assertion_executor_mocked_verilator_persists_raw_inputs(tmp_path):
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if len(cmd) > 1 and cmd[1] == "--version":
            return SimpleNamespace(returncode=0, stdout="Verilator 5.040", stderr="")
        if len(cmd) > 1 and cmd[1] == "--help":
            return SimpleNamespace(returncode=0, stdout="--assert --coverage --coverage-line", stderr="")
        if cmd[0] == "verilator_coverage":
            return SimpleNamespace(returncode=0, stdout="--summary --write-info --annotate", stderr="")
        if cmd[0] == "verilator":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    repo = EvidenceRepository(tmp_path / "evidence")
    spec = {"top_module": "dut", "interfaces": [{"name": "clk", "width": 1}],
            "verification": {"assertions": [{"id": "p", "name": "p", "expression": "1", "clock": "clk"}]}}
    result = VerilatorAssertionExecutor(runner=run).execute(
        "module dut(input clk); endmodule", "module tb; dut d(.clk(1'b0)); endmodule", "dut", spec,
        work_dir=tmp_path / "run", repository=repo)
    assert result["tool_version"] == "Verilator 5.040"
    assert result["assertions"][0]["status"] == "proven"
    assert result["artifacts"]
    assert any("--assert" in call for call in calls if call and call[0] == "verilator")


def test_coverage_executor_does_not_claim_success_without_native_data(tmp_path):
    def run(cmd, **kwargs):
        if len(cmd) > 1 and cmd[1] == "--version":
            return SimpleNamespace(returncode=0, stdout="Verilator 5.040", stderr="")
        if len(cmd) > 1 and cmd[1] == "--help":
            return SimpleNamespace(returncode=0, stdout="--coverage --coverage-line", stderr="")
        if cmd[0] == "verilator_coverage":
            return SimpleNamespace(returncode=0, stdout="--summary", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    result = VerilatorCoverageExecutor(runner=run).collect(
        "module dut; endmodule", "module tb; endmodule", "dut", work_dir=tmp_path)
    assert result["ok"] is False
    assert result["status"] == "inconclusive"
