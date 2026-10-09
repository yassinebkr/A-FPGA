from pathlib import Path
import json

from tapeout import TinyTapeoutConfig, _EVIDENCE_CONTEXT
from tapeout_repro import (
    _pdk_manifest_identity,
    TapeoutEvidenceRepository,
    build_tapeout_evidence,
    persist_tapeout_evidence,
)


WRAPPER = "module tt_um_demo(input wire clk, output wire q); assign q = clk; endmodule\n"


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    (root / "src").mkdir(parents=True)
    (root / "test").mkdir()
    (root / "runs" / "wokwi" / "44-openroad-detailedrouting").mkdir(parents=True)
    (root / "runs" / "wokwi" / "74-misc-reportmanufacturability").mkdir(parents=True)
    (root / "src" / "tt_um_demo.v").write_text(WRAPPER)
    (root / "test" / "tb.v").write_text("module tb; endmodule\n")
    (root / "src" / "config.json").write_text('{"PNR_SDC_FILE":"dir::pnr.sdc"}\n')
    (root / "src" / "user_config.json").write_text("{}\n")
    (root / "src" / "pnr.sdc").write_text("create_clock -period 20 [get_ports clk]\n")
    (root / "src" / "signoff.sdc").write_text("create_clock -period 20 [get_ports clk]\n")
    (root / "runs" / "wokwi" / "44-openroad-detailedrouting" / "state_out.json").write_text(json.dumps({
        "metrics": {"route__drc_errors__iter:0": 12, "route__drc_errors__iter:1": 0}
    }))
    (root / "runs" / "wokwi" / "74-misc-reportmanufacturability" / "state_out.json").write_text(json.dumps({
        "metrics": {
            "magic__drc_error__count": 0, "klayout__drc_error__count": 0,
            "route__drc_errors": 0,
            "design__lvs_error__count": 0,
            "design__lvs_device_difference__count": 0,
            "design__lvs_net_difference__count": 0,
            "design__lvs_property_fail__count": 0,
            "design__lvs_unmatched_device__count": 0,
            "design__lvs_unmatched_net__count": 0,
            "design__lvs_unmatched_pin__count": 0,
            "timing__setup_vio__count": 0, "timing__hold_vio__count": 0,
            "antenna__violating__nets": 0, "antenna__violating__pins": 0,
            "route__antenna_violation__count": 0,
        }
    }))
    (root / "runs" / "final.gds").write_bytes(b"gds")
    return root


def test_builder_uses_final_metrics_and_keeps_local_assurance(tmp_path):
    root = _workspace(tmp_path)
    result = {
        "status": "passed",
        "tool_output": "hardening complete; No setup violations found",
        "container": {"image": "ghcr.io/librelane/librelane:3.0.14",
                      "digest": "sha256:" + "d" * 64},
        "checks": {"klayout_drc": {"status": "passed", "metric": {"value": 0}}},
        "inputs": {"support_tools_revision": "a" * 40, "template_revision": "b" * 40},
        "artifacts": [{"path": "runs/final.gds", "sha256": "x" * 64,
                       "size_bytes": 3, "kind": "gds"}],
    }
    # Replace the intentionally synthetic artifact digest with its actual one.
    result["artifacts"][0]["sha256"] = __import__("hashlib").sha256(b"gds").hexdigest()
    run, gate = build_tapeout_evidence(
        result, wrapper_verilog=WRAPPER, top_module="demo", workspace=root,
        config=TinyTapeoutConfig(build_root=tmp_path / "build"))
    assert {item.name: item.status for item in run.stages} == {
        "drc": "passed", "lvs": "passed", "timing": "passed", "antenna": "passed",
    }
    assert run.assurance == "local_dry_run"
    assert gate.eligible is True


def test_evidence_is_canonical_and_immutable(tmp_path):
    root = _workspace(tmp_path)
    result = {
        "status": "inconclusive", "tool_output": "",
        "checks": {"klayout_drc": {"status": "inconclusive"}},
        "inputs": {},
        "artifacts": [],
    }
    repo = TapeoutEvidenceRepository(tmp_path / "evidence")
    record = persist_tapeout_evidence(
        result, wrapper_verilog=WRAPPER, top_module="demo", workspace=root,
        repository=repo)
    saved = repo.load(record["id"])
    assert saved is not None
    assert record["digest"] == __import__("hashlib").sha256(
        (tmp_path / "evidence" / "runs" / f"{record['id']}.json").read_bytes()
    ).hexdigest()
    assert saved["run"]["assurance"] == "local_dry_run"


def test_pdk_without_manifest_is_not_falsely_hashed(tmp_path):
    root = _workspace(tmp_path)
    pdk = tmp_path / "pdk"
    pdk.mkdir()
    (pdk / "huge.bin").write_bytes(b"not a manifest")
    config = TinyTapeoutConfig(build_root=tmp_path / "build", pdk_root=pdk, pdk="sky130A")
    run, _ = build_tapeout_evidence(
        {"status": "inconclusive", "checks": {}, "artifacts": []},
        wrapper_verilog=WRAPPER, top_module="demo", workspace=root, config=config)
    assert not any(item.role == "pdk" for item in run.inputs)


def test_context_is_not_left_set():
    assert _EVIDENCE_CONTEXT.get() is None


def test_ciel_pdk_identity_uses_single_pinned_revision(tmp_path):
    root = tmp_path / "ciel"
    revision = "a" * 40
    pdk = root / "ciel" / "sky130" / "versions" / revision / "sky130A"
    pdk.mkdir(parents=True)
    identity = _pdk_manifest_identity(root, "sky130A")
    assert identity is not None
    assert identity[2] == f"pdk/sky130A@{revision}"
