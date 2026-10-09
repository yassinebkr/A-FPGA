from pathlib import Path

import pytest

from tapeout_repro import (
    ArtifactInventoryItem,
    ContainerIdentity,
    HardeningStage,
    InputHash,
    TinyTapeoutRun,
    ToolIdentity,
    compare_reruns,
    evaluate_release_gate,
    hash_file,
    inventory_directory,
)


def _run(tmp_path: Path, **changes) -> TinyTapeoutRun:
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    rtl = source / "project.v"
    rtl.write_text("module project; endmodule\n", encoding="utf-8")
    values = dict(
        run_id="tt-1",
        design_id="counter8",
        top_module="tt_um_counter8",
        status="completed",
        toolchain=[ToolIdentity(name="librelane", version="3.0.3", source="tt-support-tools")],
        inputs=[InputHash(path="src/project.v", sha256=hash_file(rtl), role="rtl", size_bytes=rtl.stat().st_size)],
        config_sha256="a" * 64,
        constraints_sha256="b" * 64,
        stages=[HardeningStage(name=name, status="passed") for name in ("drc", "lvs", "timing")],
        artifacts=[ArtifactInventoryItem(path="runs/final.gds", sha256="c" * 64, size_bytes=4, kind="gds")],
    )
    values.update(changes)
    return TinyTapeoutRun(**values)


def test_inventory_is_sorted_and_hashes_bytes(tmp_path):
    root = tmp_path / "run"
    (root / "z").mkdir(parents=True)
    (root / "z" / "final.gds").write_bytes(b"gds")
    (root / "manifest.json").write_text("{}", encoding="utf-8")
    items = inventory_directory(root)
    assert [item.path for item in items] == ["manifest.json", "z/final.gds"]
    assert items[1].kind == "gds"
    assert items[1].size_bytes == 3


def test_completed_run_requires_reproducibility_inputs():
    with pytest.raises(ValueError, match="pinned tool identities"):
        TinyTapeoutRun(run_id="r", design_id="d", top_module="top", status="completed")
    with pytest.raises(ValueError, match="input hashes"):
        TinyTapeoutRun(run_id="r", design_id="d", top_module="top", status="completed",
                       toolchain=[ToolIdentity(name="tool", version="1")])


def test_gate_passes_only_with_complete_hardening_evidence(tmp_path):
    run = _run(tmp_path)
    gate = evaluate_release_gate(run)
    assert gate.eligible is True
    assert gate.reasons == []
    assert gate.evidence_digest == run.digest()


def test_gate_fails_closed_on_missing_or_inconclusive_stage(tmp_path):
    run = _run(tmp_path, stages=[HardeningStage(name="drc", status="passed")])
    gate = evaluate_release_gate(run)
    assert gate.eligible is False
    assert "required stage lvs is missing" in gate.reasons
    assert "required stage timing is missing" in gate.reasons

    run = _run(tmp_path, stages=[
        HardeningStage(name="drc", status="passed"),
        HardeningStage(name="lvs", status="inconclusive"),
        HardeningStage(name="timing", status="passed"),
    ])
    gate = evaluate_release_gate(run)
    assert gate.eligible is False
    assert "required stage lvs is inconclusive" in gate.reasons


def test_rerun_comparison_ignores_run_id_but_detects_artifact_change(tmp_path):
    first = _run(tmp_path)
    second = _run(tmp_path, run_id="tt-2", rerun_of="tt-1")
    assert compare_reruns(first, second).equivalent is True

    changed = _run(tmp_path, run_id="tt-3", artifacts=[
        ArtifactInventoryItem(path="runs/final.gds", sha256="d" * 64, size_bytes=4, kind="gds")
    ])
    comparison = compare_reruns(first, changed)
    assert comparison.equivalent is False
    assert any(item.field == "artifacts" for item in comparison.differences)
    gate = evaluate_release_gate(first, rerun=changed)
    assert gate.eligible is False
    assert gate.rerun_equivalent is False


def test_containerized_legacy_run_defaults_to_local_assurance(tmp_path):
    legacy = _run(tmp_path, container=ContainerIdentity(
        image="ghcr.io/tinytapeout/tt-tool", digest="sha256:" + "a" * 64,
    )).model_dump(mode="json")
    # Records written before TinyTapeoutRun.assurance was introduced omit it.
    legacy.pop("assurance")
    migrated = TinyTapeoutRun.model_validate(legacy)

    assert migrated.assurance == "local_dry_run"
    gate = evaluate_release_gate(migrated)
    assert gate.eligible is True
    assert gate.assurance == "local_dry_run"


def test_assurance_is_only_elevated_by_explicit_run_declaration(tmp_path):
    run = _run(tmp_path,
               container=ContainerIdentity(
                   image="ghcr.io/tinytapeout/tt-tool", digest="sha256:" + "a" * 64,
               ),
               assurance="provider_hardening")
    gate = evaluate_release_gate(run)
    assert gate.assurance == "provider_hardening"


def test_container_digest_is_pinned():
    identity = ContainerIdentity(image="ghcr.io/tinytapeout/tt-tool", digest="sha256:" + "a" * 64)
    assert identity.digest.startswith("sha256:")
    with pytest.raises(ValueError):
        ContainerIdentity(image="tool", digest="latest")
