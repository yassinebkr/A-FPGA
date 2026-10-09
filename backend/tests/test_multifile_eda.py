import hashlib

import pytest

import hardware_project
import multifile_eda
from evidence import EvidenceRepository


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    monkeypatch.setattr(hardware_project, "repository", repo)
    update = hardware_project.HardwareProjectUpdate.model_validate({
        "top_module": "top",
        "files": [
            {"path": "rtl/pkg.sv", "role": "package",
             "content": "package p; parameter int W=1; endpackage\n"},
            {"path": "rtl/top.sv", "role": "rtl",
             "content": "module top(input logic a, output logic y); assign y=a; endmodule\n"},
            {"path": "rtl/defs.svh", "role": "header", "content": "macro X 1\n"},
            {"path": "tb/top_tb.sv", "role": "testbench",
             "content": "module top_tb; endmodule\n"},
        ],
        "compile_order": ["rtl/pkg.sv", "rtl/top.sv"],
        "include_dirs": ["rtl"],
        "defines": {"SYNTHESIS": None, "WIDTH": "32"},
        "testbenches": ["tb/top_tb.sv"],
        "provenance": {"upstream": "https://example.invalid/core", "commit": "a" * 40},
    })
    return hardware_project.build_manifest(update)


def test_materializes_exact_order_and_content(manifest, tmp_path):
    project = multifile_eda.materialize_manifest(manifest, destination=tmp_path / "work")
    assert [path.relative_to(project.root).as_posix() for path in project.sources] == [
        "rtl/pkg.sv", "rtl/top.sv"
    ]
    for entry in manifest["files"]:
        body = (project.root / entry["path"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == entry["sha256"]
    assert project.defines == (("SYNTHESIS", None), ("WIDTH", "32"))


def test_verilator_argv_preserves_project_metadata(manifest, tmp_path):
    project = multifile_eda.materialize_manifest(manifest, destination=tmp_path / "work")
    argv = multifile_eda.verilator_lint_argv(project)
    assert argv[-2:] == [str(project.root / "rtl/pkg.sv"), str(project.root / "rtl/top.sv")]
    assert f"-I{project.root / 'rtl'}" in argv
    assert "-DSYNTHESIS" in argv
    assert "-DWIDTH=32" in argv
    assert argv[argv.index("--top-module") + 1] == "top"


def test_refuses_symlink_target(manifest, tmp_path):
    root = tmp_path / "work" / manifest["digest"]
    (root / "rtl").mkdir(parents=True)
    (root / "rtl/top.sv").symlink_to(tmp_path / "outside")
    with pytest.raises(multifile_eda.MultiFileEdaError, match="symlink"):
        multifile_eda.materialize_manifest(manifest, destination=tmp_path / "work")
