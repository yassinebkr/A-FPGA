import hashlib
import json
import subprocess

import pytest

import hardware_project
import sv2v_preprocess
from evidence import EvidenceRepository


@pytest.fixture
def manifest_and_repo(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    monkeypatch.setattr(hardware_project, "repository", repo)
    update = hardware_project.HardwareProjectUpdate.model_validate({
        "top_module": "soc_top",
        "files": [
            {"path": "rtl/pkg.sv", "role": "package",
             "content": "package soc_pkg; parameter int W=32; endpackage\n"},
            {"path": "rtl/alu.sv", "role": "rtl",
             "content": "module alu(input logic a, output logic y); assign y=a; endmodule\n"},
            {"path": "rtl/soc_top.sv", "role": "rtl",
             "content": "module soc_top(input logic a, output logic y); alu u_alu(.a(a), .y(y)); endmodule\n"},
            {"path": "rtl/inc/defs.svh", "role": "header",
             "content": "`define SOC_FEATURE 1\n"},
            {"path": "tb/soc_top_tb.sv", "role": "testbench",
             "content": "module soc_top_tb; endmodule\n"},
        ],
        "compile_order": ["rtl/pkg.sv", "rtl/alu.sv", "rtl/soc_top.sv"],
        "include_dirs": ["rtl/inc"],
        "defines": {"SYNTHESIS": None, "WIDTH": "32"},
        "testbenches": ["tb/soc_top_tb.sv"],
        "provenance": {"upstream": "fixture", "commit": "a" * 40},
    })
    return hardware_project.build_manifest(update), repo


@pytest.fixture
def config():
    return sv2v_preprocess.TrustedSv2vConfig(
        container_image="ghcr.io/example/sv2v@sha256:" + "a" * 64,
        tool_identity="sv2v 0.9.0 fixture",
    )


def test_digest_bound_report_preserves_hierarchy_and_argv(
        manifest_and_repo, config, tmp_path):
    manifest, repo = manifest_and_repo

    def fake_runner(argv, *, cwd, timeout):
        assert argv[0:4] == ["docker", "run", "--rm", "--network=none"]
        assert "--read-only" in argv
        assert config.container_image in argv
        assert "-I/input/rtl/inc" in argv
        assert "-DSYNTHESIS" in argv
        assert "-DWIDTH=32" in argv
        (cwd / "soc_top.v").write_text(
            "module soc_top(input wire a, output wire y); assign y = a; endmodule\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "sv2v ok\n", "")

    report = sv2v_preprocess.preprocess_manifest(
        manifest, config=config, destination=tmp_path / "work", runner=fake_runner)

    assert report["schema"] == "hardware-project-sv2v-report/v1"
    assert report["input_manifest_digest"] == manifest["digest"]
    assert report["hardware_project_digest"] == manifest["digest"]
    assert report["tool_identity"] == config.tool_identity
    assert report["output_sha256"] == report["output_artifact"]["sha256"]
    assert report["report_digest"] == sv2v_preprocess._digest(
        {key: value for key, value in report.items() if key != "report_digest"})
    assert report["source_hierarchy"]["compile_order"] == manifest["compile_order"]
    assert [item["path"] for item in report["source_hierarchy"]["files"]] == [
        item["path"] for item in manifest["files"]
    ]
    assert report["equivalence"] == {
        "status": "not_checked",
        "claim": False,
        "note": "sv2v preprocessing is a representation conversion; equivalence requires a separate verification gate.",
    }
    assert repo.get_artifact(report["output_sha256"]).startswith(b"module soc_top")


def test_manifest_digest_and_artifacts_are_verified_before_runner(
        manifest_and_repo, config, tmp_path):
    manifest, _ = manifest_and_repo
    tampered = json.loads(json.dumps(manifest))
    tampered["include_dirs"] = []

    called = False

    def fake_runner(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("runner must not execute for a tampered manifest")

    with pytest.raises(sv2v_preprocess.Sv2vPreprocessError,
                       match="manifest digest mismatch"):
        sv2v_preprocess.preprocess_manifest(
            tampered, config=config, destination=tmp_path / "work", runner=fake_runner)
    assert called is False


def test_runner_failure_and_missing_output_fail_closed(
        manifest_and_repo, config, tmp_path):
    manifest, _ = manifest_and_repo

    def failed(argv, *, cwd, timeout):
        return subprocess.CompletedProcess(argv, 17, "", "syntax error")

    with pytest.raises(sv2v_preprocess.Sv2vPreprocessError, match="exit 17"):
        sv2v_preprocess.preprocess_manifest(
            manifest, config=config, destination=tmp_path / "failed", runner=failed)

    def no_output(argv, *, cwd, timeout):
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(sv2v_preprocess.Sv2vPreprocessError, match="without a regular"):
        sv2v_preprocess.preprocess_manifest(
            manifest, config=config, destination=tmp_path / "missing", runner=no_output)


def test_output_symlink_and_unpinned_config_are_rejected(
        manifest_and_repo, config, tmp_path):
    manifest, _ = manifest_and_repo
    with pytest.raises(ValueError, match="pinned"):
        sv2v_preprocess.TrustedSv2vConfig(
            container_image="ghcr.io/example/sv2v:latest",
            tool_identity="sv2v fixture",
        )

    output_dir = tmp_path / "work" / f".{manifest['digest']}.sv2v-output"
    output_dir.mkdir(parents=True)
    (output_dir / "soc_top.v").symlink_to(tmp_path / "outside.v")

    def fake_runner(*args, **kwargs):
        raise AssertionError("symlink must be rejected before execution")

    with pytest.raises(sv2v_preprocess.Sv2vPreprocessError, match="output"):
        sv2v_preprocess.preprocess_manifest(
            manifest, config=config, destination=tmp_path / "work", runner=fake_runner)


def test_no_shell_and_source_store_remains_unchanged(
        manifest_and_repo, config, tmp_path, monkeypatch):
    manifest, repo = manifest_and_repo
    original = {
        entry["sha256"]: repo.get_artifact(entry["sha256"])
        for entry in manifest["files"]
    }
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        kwargs["cwd"].joinpath("soc_top.v").write_bytes(b"module soc_top; endmodule\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sv2v_preprocess, "_run_argv", fake_run)
    report = sv2v_preprocess.preprocess_manifest(
        manifest, config=config, destination=tmp_path / "work")
    assert captured["argv"][0:2] == ["docker", "run"]
    assert captured["kwargs"]["timeout"] == 300
    assert all(captured["argv"][index] != "--shell"
               for index in range(len(captured["argv"])))
    assert all(repo.get_artifact(digest) == body for digest, body in original.items())
    assert hashlib.sha256(repo.get_artifact(report["output_sha256"])).hexdigest() == report["output_sha256"]

