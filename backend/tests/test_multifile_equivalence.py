import subprocess

import pytest

import hardware_project
from evidence import EvidenceRepository
from hardware_project import HardwareProjectUpdate, SourceInput
from multifile_equivalence import (
    ConversionEquivalenceError, EquivalenceConfig, run_conversion_equivalence,
)


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    monkeypatch.setattr(hardware_project, "repository", repo)
    return hardware_project.build_manifest(HardwareProjectUpdate(
        top_module="top",
        files=[SourceInput(path="rtl/top.sv", content="module top(input logic a, output logic y); assign y=a; endmodule\n")],
        defines={"SYNTHESIS": "1"},
    ))


def _config():
    return EquivalenceConfig("ghcr.io/example/tool@sha256:" + "a" * 64, "yosys fixture")


def test_independent_frontends_and_digest_binding(manifest, tmp_path):
    candidate = tmp_path / "candidate.v"
    candidate.write_text("module top(input a, output y); assign y=a; endmodule\n")
    seen = {}
    def runner(argv, *, cwd, timeout):
        seen["argv"] = argv
        script = (cwd / "equivalence.ys").read_text()
        assert "read_slang /gold/rtl/top.sv --top top -DSYNTHESIS=1" in script
        assert "read_verilog -formal -sv /gate/candidate.v" in script
        assert script.count("async2sync") == 2
        assert script.count("memory_map") == 2
        return subprocess.CompletedProcess(argv, 0, "Equivalence successfully proven!\n", "")
    result = run_conversion_equivalence(
        manifest, candidate, config=_config(), destination=tmp_path / "work", runner=runner)
    assert result["status"] == "proven"
    assert seen["argv"][0:4] == ["docker", "run", "--rm", "--network=none"]


def test_candidate_digest_mismatch_fails_before_runner(manifest, tmp_path):
    candidate = tmp_path / "candidate.v"
    candidate.write_text("module top; endmodule\n")
    with pytest.raises(ConversionEquivalenceError, match="digest mismatch"):
        run_conversion_equivalence(manifest, candidate, config=_config(),
                                   destination=tmp_path / "work",
                                   expected_converted_sha256="0" * 64,
                                   runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
