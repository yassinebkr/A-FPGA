import asyncio
import json
import os
import sys
import types
from pathlib import Path

import pytest

import differential_regression as dr
from golden_models import RegressionCorpus, seed_case


def test_cocotb2_runner_module_layout_is_preferred(monkeypatch):
    parent = types.ModuleType("cocotb_tools")
    parent.__path__ = []
    runner_module = types.ModuleType("cocotb_tools.runner")
    expected = lambda simulator: simulator
    runner_module.get_runner = expected
    monkeypatch.setitem(sys.modules, "cocotb_tools", parent)
    monkeypatch.setitem(sys.modules, "cocotb_tools.runner", runner_module)
    assert dr._get_cocotb_runner() is expected


def test_legacy_cocotb_runner_layout_remains_supported(monkeypatch):
    parent = types.ModuleType("cocotb")
    parent.__path__ = []
    runner_module = types.ModuleType("cocotb.runner")
    expected = lambda simulator: simulator
    runner_module.get_runner = expected
    monkeypatch.setitem(sys.modules, "cocotb", parent)
    monkeypatch.setitem(sys.modules, "cocotb.runner", runner_module)
    monkeypatch.delitem(sys.modules, "cocotb_tools.runner", raising=False)
    monkeypatch.delitem(sys.modules, "cocotb_tools", raising=False)
    assert dr._get_cocotb_runner() is expected


def test_default_interfaces_are_explicit_and_symbolic_inputs_are_encoded():
    interface = dr.interface_for_model("alu8")
    assert interface.inputs == ("a", "b", "op")
    assert interface.outputs == ("y", "carry", "zero")
    assert interface.input_encodings["op"]["xor"] == 4
    assert dr.DutInterface.from_json(interface.to_json()) == interface


def test_reference_failure_is_reported_before_a_simulator_job(tmp_path):
    corpus = RegressionCorpus(tmp_path / "corpus")
    case = seed_case("alu8", count=4)
    stale = type(case)(case.case_id, case.model, "0.0.0", case.vectors)
    corpus.save(stale)
    report = asyncio.run(dr.run_differential_regression_report(
        dr.DifferentialRunConfig("dut", "module dut; endmodule", "alu8",
                                 tmp_path / "corpus")
    ))
    assert report["status"] == "reference_failure"
    assert report["job_id"] is None
    assert "reference version" in report["reason"]


def test_missing_tools_is_inconclusive_and_leaves_audit_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path / "builds"))
    monkeypatch.setattr(dr.shutil, "which", lambda _name: None)
    monkeypatch.setattr(dr, "_cocotb_version", lambda: None)
    report = asyncio.run(dr.run_differential_regression_report(
        dr.DifferentialRunConfig("dut", "module dut; endmodule", "alu8",
                                 Path(__file__).parents[1] / "regression_corpus")
    ))
    assert report["status"] == "inconclusive"
    assert Path(report["artifacts"]["rtl"]).read_text() == "module dut; endmodule"
    assert Path(report["artifacts"]["case"]).exists()
    manifest = Path(report["artifacts"]["manifest"])
    assert json.loads(manifest.read_text())["schema_version"] == "differential-regression/v1"


def test_workspace_rejects_unsafe_generated_top(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    config = dr.DifferentialRunConfig("../escape", "module dut; endmodule", "alu8",
                                      Path(__file__).parents[1] / "regression_corpus")
    with pytest.raises(ValueError):
        asyncio.run(dr.run_differential_regression_report(config))


def test_generated_cocotb_test_uses_delta_safe_settle_and_closes_inputs():
    source = dr._cocotb_test_source()
    assert "output_mismatch" in source
    assert "runtime_failure" in source
    assert "int(value)" in source
    assert "await Timer(1, unit=\"step\")" in source
    assert "ReadWrite" not in source
    assert "ReadOnly" not in source
    assert "units=" not in source
    assert 'with open(os.environ["DIFF_CASE_PATH"]' in source
    assert 'with open(os.environ["DIFF_INTERFACE_PATH"]' in source


def test_each_cocotb_invocation_sets_its_own_report_path(tmp_path, monkeypatch):
    class FakeRunner:
        def build(self, **_kwargs):
            return None

        def test(self, **_kwargs):
            return None

    monkeypatch.setattr(dr, "_get_cocotb_runner", lambda: lambda _sim: FakeRunner())
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    first = run_dir / "verilator.json"
    second = run_dir / "icarus.json"
    dr._run_cocotb("verilator", run_dir=run_dir, top_module="dut", report_path=first)
    assert os.environ["DIFF_REPORT_PATH"] == str(first.resolve())
    dr._run_cocotb("icarus", run_dir=run_dir, top_module="dut", report_path=second)
    assert os.environ["DIFF_REPORT_PATH"] == str(second.resolve())
    assert json.loads(first.read_text())["status"] == "runtime_failure"
    assert json.loads(second.read_text())["status"] == "runtime_failure"
