"""Phase 2 boundary tests for multi-file project execution metadata.

These tests intentionally stop at the project-manifest boundary.  They do not
invoke a model or an EDA tool: execution adapters must consume this exact,
immutable source-set description in a later Phase 2 integration task.
"""

import hashlib
import json

import pytest

import hardware_project
from evidence import EvidenceRepository


@pytest.fixture
def isolated_repository(tmp_path, monkeypatch):
    repository = EvidenceRepository(tmp_path / "evidence")
    monkeypatch.setattr(hardware_project, "repository", repository)
    return repository


def _update():
    return hardware_project.HardwareProjectUpdate.model_validate({
        "top_module": "ibex_top",
        "files": [
            {"path": "rtl/ibex_pkg.sv", "role": "package",
             "content": "package ibex_pkg; parameter XLEN = 32; endpackage\n"},
            {"path": "rtl/include/rv_defs.svh", "role": "header",
             "content": "`define RV32I 1\n"},
            {"path": "rtl/ibex_core.sv", "role": "rtl",
             "content": "module ibex_core; endmodule\n"},
            {"path": "rtl/ibex_top.sv", "role": "rtl",
             "content": "module ibex_top; ibex_core u_core(); endmodule\n"},
            {"path": "tb/ibex_tb.sv", "role": "testbench",
             "content": "module ibex_tb; endmodule\n"},
            {"path": "constraints/ibex.sdc", "role": "constraint",
             "content": "create_clock -period 20 [get_ports clk]\n"},
        ],
        # Package must be compiled before the consumers; headers are included,
        # not standalone compilation units.
        "compile_order": [
            "rtl/ibex_pkg.sv", "rtl/ibex_core.sv", "rtl/ibex_top.sv",
        ],
        "include_dirs": ["rtl/include"],
        "defines": {"RV32I": None, "IBEX_CFG": "rv32imc"},
        "testbenches": ["tb/ibex_tb.sv"],
        "constraints": ["constraints/ibex.sdc"],
        "build_targets": {"sky130": {"target": "skywater130_demo",
                                       "clock_period_ns": 20}},
    })


def test_execution_inputs_preserve_order_includes_defines_and_roles(
        isolated_repository):
    manifest = hardware_project.build_manifest(_update())

    assert manifest["compile_order"] == [
        "rtl/ibex_pkg.sv", "rtl/ibex_core.sv", "rtl/ibex_top.sv",
    ]
    assert manifest["include_dirs"] == ["rtl/include"]
    assert manifest["defines"] == {"RV32I": None, "IBEX_CFG": "rv32imc"}
    assert manifest["testbenches"] == ["tb/ibex_tb.sv"]
    assert manifest["constraints"] == ["constraints/ibex.sdc"]
    assert [entry["path"] for entry in manifest["files"]] == [
        "rtl/ibex_pkg.sv", "rtl/include/rv_defs.svh", "rtl/ibex_core.sv",
        "rtl/ibex_top.sv", "tb/ibex_tb.sv", "constraints/ibex.sdc",
    ]
    assert [entry["role"] for entry in manifest["files"]] == [
        "package", "header", "rtl", "rtl", "testbench", "constraint",
    ]


def test_hydration_returns_exact_source_bodies_from_content_store(
        isolated_repository):
    manifest = hardware_project.build_manifest(_update())
    hydrated = hardware_project.hydrate_manifest(manifest)

    expected = {source.path: source.content for source in _update().files}
    assert {entry["path"]: entry["content"] for entry in hydrated["files"]} == expected
    for entry in hydrated["files"]:
        body = entry["content"].encode()
        assert entry["sha256"] == hashlib.sha256(body).hexdigest()
        assert entry["size"] == len(body)
    assert hydrated["digest"] == manifest["digest"]


def test_digest_binds_source_set_and_execution_metadata(isolated_repository):
    base = hardware_project.build_manifest(_update())
    changed = _update().model_copy(deep=True)
    changed.defines["IBEX_CFG"] = "rv32im"
    altered = hardware_project.build_manifest(changed)
    assert altered["digest"] != base["digest"]

    reordered = _update().model_copy(deep=True)
    reordered.compile_order = [
        "rtl/ibex_pkg.sv", "rtl/ibex_top.sv", "rtl/ibex_core.sv",
    ]
    reordered_manifest = hardware_project.build_manifest(reordered)
    assert reordered_manifest["digest"] != base["digest"]

    # A digest is over canonical manifest metadata, not only the source bytes.
    tampered = json.loads(json.dumps(base))
    tampered["include_dirs"] = []
    with pytest.raises(hardware_project.HardwareProjectError,
                       match="manifest digest mismatch"):
        hardware_project.validate_manifest(tampered)


def test_missing_or_corrupt_artifact_fails_closed_during_hydration(
        isolated_repository):
    manifest = hardware_project.build_manifest(_update())

    missing = json.loads(json.dumps(manifest))
    missing["files"][0]["sha256"] = "0" * 64
    missing["digest"] = hardware_project._digest({
        key: value for key, value in missing.items() if key != "digest"
    })
    with pytest.raises(hardware_project.HardwareProjectError,
                       match="missing artifact"):
        hardware_project.validate_manifest(missing)

    corrupt = json.loads(json.dumps(manifest))
    digest = corrupt["files"][0]["sha256"]
    isolated_repository.put_artifact(b"not the package", name="corrupt.sv")
    # Replace the stored bytes behind the referenced digest to model disk
    # corruption without changing the manifest's source-set identity.
    path = isolated_repository.artifacts / digest[:2] / digest
    path.write_bytes(b"corrupt")
    with pytest.raises(hardware_project.HardwareProjectError,
                       match="artifact mismatch"):
        hardware_project.validate_manifest(corrupt)


def test_invalid_compile_order_is_rejected_before_any_manifest_is_built(
        isolated_repository):
    raw = _update().model_dump()
    raw["compile_order"] = ["rtl/ibex_top.sv", "rtl/ibex_top.sv"]
    with pytest.raises(ValueError, match="compile_order"):
        hardware_project.HardwareProjectUpdate.model_validate(raw)

    raw = _update().model_dump()
    raw["compile_order"] = [
        "rtl/ibex_pkg.sv", "rtl/ibex_core.sv", "rtl/ibex_top.sv",
        "rtl/include/rv_defs.svh",
    ]
    with pytest.raises(ValueError, match="compile_order"):
        hardware_project.HardwareProjectUpdate.model_validate(raw)
