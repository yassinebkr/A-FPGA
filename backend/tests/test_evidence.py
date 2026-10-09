from datetime import datetime, timezone
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import evidence_api
from constraints import ConstraintSet
from evidence import (
    ArtifactRef,
    ClockDomain,
    EvidenceAssurance,
    DesignSpec,
    EvidenceRepository,
    ImmutableRunError,
    InterfaceSpec,
    MetricValue,
    Requirement,
    RequirementResult,
    RunManifest,
    StageResult,
    build_evidence_report,
    canonical_json,
    sha256_record,
)


def _spec() -> DesignSpec:
    return DesignSpec(
        design_id="mac",
        revision=1,
        name="Small MAC",
        top_module="mac_top",
        target="fpga-demo",
        interfaces=[InterfaceSpec(name="a", direction="input", width=8)],
        clock_domains=[ClockDomain(name="core", clock_port="clk", frequency_mhz=50)],
        requirements=[Requirement(id="functional", text="computes the product")],
    )


def _manifest(spec: DesignSpec, **changes) -> RunManifest:
    values = dict(
        run_id="run-test",
        design_id=spec.design_id,
        design_revision=spec.revision,
        spec_sha256=sha256_record(spec),
        target=spec.target,
        status="planned",
    )
    values.update(changes)
    return RunManifest(**values)


def test_canonical_json_is_stable_and_hashable():
    first = canonical_json({"b": 2, "a": 1})
    second = canonical_json({"a": 1, "b": 2})
    assert first == second == '{"a":1,"b":2}'
    assert sha256_record({"a": 1}) == sha256_record({"a": 1})


def test_design_spec_rejects_nonportable_top_and_version():
    with pytest.raises(ValueError):
        DesignSpec(design_id="d", name="D", top_module="../bad")
    with pytest.raises(ValueError):
        DesignSpec(design_id="d", name="D", top_module="top", schema_version="2.0")


def test_completed_manifest_requires_timestamp():
    spec = _spec()
    with pytest.raises(ValueError):
        _manifest(spec, status="completed")
    manifest = _manifest(spec, status="completed",
                         completed_at=datetime.now(timezone.utc))
    assert manifest.status == "completed"


def test_repository_round_trip_and_immutable_completed_run(tmp_path):
    repo = EvidenceRepository(tmp_path / "evidence")
    spec = _spec()
    digest = repo.save_spec(spec)
    assert len(digest) == 64
    assert repo.load_spec("mac", 1) == spec

    manifest = _manifest(spec, status="completed",
                         completed_at=datetime.now(timezone.utc))
    repo.save_manifest(manifest)
    assert repo.load_manifest(manifest.run_id) == manifest
    with pytest.raises(ImmutableRunError):
        repo.save_manifest(manifest.model_copy(update={"warnings": ["changed"]}))


def test_repository_allows_planned_to_completed_transition(tmp_path):
    repo = EvidenceRepository(tmp_path)
    spec = _spec()
    manifest = _manifest(spec)
    repo.save_manifest(manifest)
    finished = manifest.model_copy(update={
        "status": "completed",
        "completed_at": datetime.now(timezone.utc),
    })
    repo.save_manifest(finished)
    assert repo.load_manifest(manifest.run_id).status == "completed"


def test_artifact_storage_is_content_addressed_and_deduplicated(tmp_path):
    repo = EvidenceRepository(tmp_path)
    first = repo.put_artifact(b"rtl", name="design.v", media_type="text/plain")
    second = repo.put_artifact(b"rtl", name="same-content.v")
    assert first.sha256 == second.sha256
    assert first.size_bytes == 3
    assert repo.get_artifact(first.sha256) == b"rtl"
    assert (tmp_path / first.locator).is_file()


def test_report_is_conservative_when_mandatory_evidence_is_missing():
    spec = _spec()
    manifest = _manifest(spec, status="completed",
                         completed_at=datetime.now(timezone.utc))
    report = build_evidence_report(manifest, spec)
    assert report.overall_status == "inconclusive"
    assert report.confidence == "exploratory"
    assert "functional" in report.limitations[0] or report.requirements[0].status == "not_tested"


def test_report_pass_and_fpga_confidence_requires_recorded_evidence():
    spec = _spec()
    manifest = _manifest(
        spec,
        status="completed",
        completed_at=datetime.now(timezone.utc),
        stages=[StageResult(name="hardware", status="passed")],
        requirement_results=[RequirementResult(
            requirement_id="functional", status="passed",
            evidence=["hardware-vector-001"],
        )],
        metrics=[MetricValue(name="fmax", value=50, unit="MHz", stage="timing",
                             target="fpga-demo", corner="typical", tool="nextpnr",
                             tool_version="1.0")],
    )
    report = build_evidence_report(manifest, spec)
    assert report.overall_status == "passed"
    assert report.confidence == "fpga_validated"


def test_expert_implementation_report_is_explicitly_unavailable_without_data():
    spec = _spec()
    report = build_evidence_report(_manifest(spec), spec)
    assert report.implementation.status == "unavailable"
    assert report.implementation.timing.status == "unavailable"
    assert report.implementation.area.status == "unavailable"
    assert report.implementation.power.status == "unavailable"
    assert report.implementation.artifact_refs == []
    assert "power is not measured" in report.implementation.limitations[-1]


def test_expert_implementation_report_preserves_timing_area_power_and_tool_evidence():
    spec = _spec()
    manifest = _manifest(
        spec,
        toolchain={"yosys": "0.50", "opensta": "3.1.0"},
        constraints={"clock_period_ns": 10.0, "clock_port": "clk"},
        metrics=[
            MetricValue(name="setupslack", value="-0.25 ns", unit="ns", stage="timing", target="sky130", corner="typical", tool="OpenSTA", tool_version="3.1.0"),
            MetricValue(name="fmax", value=80_000_000, unit="Hz", stage="timing", target="sky130", corner="typical", tool="OpenSTA", tool_version="3.1.0"),
            MetricValue(name="cellarea", value=120.5, unit="um^2", stage="synthesis", target="sky130", corner="typical", tool="Yosys", tool_version="0.50"),
            MetricValue(name="utilization", value=42.0, unit="%", stage="place", target="sky130", corner="typical", tool="OpenROAD", tool_version="26Q2"),
            MetricValue(name="power", value=2.5, unit="mW", stage="power", target="sky130", corner="typical", tool="OpenSTA", tool_version="3.1.0"),
        ],
        metadata={"timing": {"paths": [{"startpoint": "u0/q", "endpoint": "u1/d", "analysis_type": "setup", "slack_ns": -0.25, "delay_ns": 10.25}]},
                  "power": {"assumptions": ["activity factor = 0.2"], "voltage_v": 1.8, "frequency_mhz": 80}},
    )
    report = build_evidence_report(manifest, spec)
    implementation = report.implementation
    assert implementation.status == "available"
    assert implementation.timing.status == "available"
    assert implementation.timing.fmax_mhz == 80.0
    assert implementation.timing.paths[0].endpoint == "u1/d"
    assert implementation.area.cell_area_um2 == 120.5
    assert implementation.area.utilization_percent == 42.0
    assert implementation.power.total_mw == 2.5
    assert implementation.power.assumptions == ["activity factor = 0.2"]
    assert [(item.tool, item.version) for item in implementation.tool_versions] == [("yosys", "0.50"), ("opensta", "3.1.0")]



def _passed_functional_result():
    return RequirementResult(requirement_id="functional", status="passed",
                             evidence=["vector-001"])


def test_assurance_classifies_simulation_without_overclaiming():
    spec = _spec()
    manifest = _manifest(
        spec, status="completed", completed_at=datetime.now(timezone.utc),
        stages=[StageResult(name="simulation", status="passed", tool="verilator")],
        requirement_results=[_passed_functional_result()],
    )
    report = build_evidence_report(manifest, spec)
    assert report.assurance == EvidenceAssurance.SIMULATION
    assert report.metrics == []
    assert "simulation" in report.evidence_classes
    assert any("does not establish" in item for item in report.limitations)


def test_assurance_distinguishes_fpga_implementation():
    spec = _spec()
    manifest = _manifest(
        spec, status="completed", completed_at=datetime.now(timezone.utc),
        target="xilinx-artix7",
        stages=[StageResult(name="fpga_implementation", status="passed", tool="nextpnr")],
        metrics=[MetricValue(name="fmax", value=50, unit="MHz", stage="timing",
                             target="xilinx-artix7", corner="typical", tool="nextpnr",
                             tool_version="1.0")],
        requirement_results=[_passed_functional_result()],
    )
    report = build_evidence_report(manifest, spec)
    assert report.assurance == EvidenceAssurance.FPGA_IMPLEMENTATION
    assert report.metrics[0].assurance == EvidenceAssurance.FPGA_IMPLEMENTATION
    assert any("does not establish ASIC" in item for item in report.limitations)


def test_demo_pdk_never_becomes_foundry_signoff_from_stage_names():
    spec = _spec()
    manifest = _manifest(
        spec, status="completed", completed_at=datetime.now(timezone.utc),
        target="skywater130_demo",
        stages=[StageResult(name=name, status="passed")
                for name in ("drc", "lvs", "timing_signoff")],
        metrics=[MetricValue(name="fmax", value=50, unit="MHz", stage="timing",
                             target="skywater130_demo", corner="typical", tool="OpenSTA",
                             tool_version="3.1")],
        requirement_results=[_passed_functional_result()],
    )
    report = build_evidence_report(manifest, spec)
    assert report.assurance == EvidenceAssurance.DEMO_PDK_ESTIMATE
    assert report.confidence == "exploratory"
    assert EvidenceAssurance.FOUNDRY_SIGNOFF not in report.evidence_classes
    assert report.metrics[0].assurance == EvidenceAssurance.DEMO_PDK_ESTIMATE
    assert any("not FPGA measurements or foundry signoff" in item
               for item in report.limitations)


def test_foundry_signoff_requires_explicit_approved_provenance():
    spec = _spec()
    manifest = _manifest(
        spec, status="completed", completed_at=datetime.now(timezone.utc),
        target="vendor-process",
        stages=[StageResult(name=name, status="passed")
                for name in ("drc", "lvs", "timing_signoff")],
        metrics=[MetricValue(name="fmax", value=50, unit="MHz", stage="timing",
                             target="vendor-process", corner="typical", tool="STA",
                             tool_version="1.0")],
        requirement_results=[_passed_functional_result()],
        metadata={"assurance": "foundry_signoff", "signoff": {
            "approved_pdk": "vendor-pdk-1", "rule_deck": "deck-1",
            "independent_review": True}},
    )
    report = build_evidence_report(manifest, spec)
    assert report.assurance == EvidenceAssurance.FOUNDRY_SIGNOFF
    assert report.confidence == "manufacturing_candidate"
    assert report.metrics[0].assurance == EvidenceAssurance.FOUNDRY_SIGNOFF
    assert report.assurance_basis

def test_yaml_round_trip_uses_safe_loader(tmp_path):
    repo = EvidenceRepository(tmp_path)
    spec = _spec()
    path = tmp_path / "spec.yaml"
    repo.save_yaml(path, spec)
    loaded = repo.load_yaml(path, DesignSpec)
    assert loaded == spec


def test_iter_manifests_skips_corrupt_records(tmp_path):
    repo = EvidenceRepository(tmp_path)
    spec = _spec()
    repo.save_manifest(_manifest(spec))
    repo.runs.joinpath("run-corrupt.json").write_text("not-json", encoding="utf-8")
    assert [item.run_id for item in repo.iter_manifests()] == ["run-test"]



def test_manifest_rejects_unattached_stage_artifact():
    spec = _spec()
    with pytest.raises(ValueError, match="not present in artifacts"):
        _manifest(spec, stages=[StageResult(
            name="lint", status="passed", artifact_hashes=["a" * 64])])


def test_manifest_accepts_artifacts_referenced_by_evidence():
    spec = _spec()
    artifact = ArtifactRef(sha256="a" * 64, name="lint.json", size_bytes=1,
                           locator="artifacts/sha256/aa/" + "a" * 64)
    manifest = _manifest(spec, artifacts=[artifact], stages=[StageResult(
        name="lint", status="passed", artifact_hashes=[artifact.sha256])])
    assert manifest.stages[0].artifact_hashes == [artifact.sha256]


def test_api_requires_stored_spec_and_matching_hash_and_parent(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path)
    monkeypatch.setattr(evidence_api, "repository", repo)
    app = FastAPI()
    app.include_router(evidence_api.router)
    spec = _spec()
    body = _manifest(spec).model_dump(mode="json")
    with TestClient(app) as client:
        missing = client.post("/api/evidence/runs", json=body)
        assert missing.status_code == 404

        saved = client.post("/api/evidence/specs", json=spec.model_dump(mode="json"))
        assert saved.status_code == 201
        mismatch = dict(body, spec_sha256="b" * 64)
        assert client.post("/api/evidence/runs", json=mismatch).status_code == 409

        parent_missing = dict(body, parent_run_id="run-does-not-exist")
        assert client.post("/api/evidence/runs", json=parent_missing).status_code == 404

        valid = client.post("/api/evidence/runs", json=body)
        assert valid.status_code == 201
        assert valid.json()["report"]["overall_status"] == "not_tested"
        implementation = client.get("/api/evidence/runs/run-test/implementation")
        assert implementation.status_code == 200
        assert implementation.json()["status"] == "unavailable"
        assurance = client.get("/api/evidence/runs/run-test/assurance")
        assert assurance.status_code == 200
        assert assurance.json()["assurance"] == "unclassified"
        assert assurance.json()["evidence_classes"] == ["unclassified"]


def test_api_exposes_typed_constraint_contract_and_hash(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path)
    monkeypatch.setattr(evidence_api, "repository", repo)
    app = FastAPI()
    app.include_router(evidence_api.router)
    spec = _spec()
    with TestClient(app) as client:
        saved = client.post("/api/evidence/specs", json=spec.model_dump(mode="json"))
        assert saved.status_code == 201
        response = client.get("/api/evidence/specs/mac/1/constraints")
    assert response.status_code == 200
    payload = response.json()
    assert payload["constraints_sha256"] == spec.constraints.content_sha256()
    assert payload["constraints"]["schema_version"] == "constraints/v1"

def test_constraint_artifact_is_exactly_rendered_and_linked(tmp_path):
    repo = EvidenceRepository(tmp_path)
    spec = _spec().model_copy(update={"constraints": ConstraintSet(
        clocks=[{"name": "clk", "port": "clk", "period_ns": 10.0}]
    )})
    artifact = repo.put_constraint_artifact(spec.constraints, name="mac.sdc", format="sdc")
    manifest = _manifest(spec, constraints=spec.constraints.canonical_data(),
                          constraints_sha256=spec.constraints.content_sha256(),
                          constraints_artifact=artifact, artifacts=[artifact])
    repo.save_manifest(manifest)
    assert repo.get_artifact(artifact.sha256) == spec.constraints.to_sdc().encode()
    report = build_evidence_report(manifest, spec)
    assert report.constraints_sha256 == spec.constraints.content_sha256()
    assert report.constraints_artifact.sha256 == artifact.sha256


def test_constraint_artifact_rejects_tampering_or_mismatched_declaration(tmp_path):
    repo = EvidenceRepository(tmp_path)
    spec = _spec().model_copy(update={"constraints": ConstraintSet(
        clocks=[{"name": "clk", "port": "clk", "period_ns": 10.0}]
    )})
    artifact = repo.put_constraint_artifact(spec.constraints, name="mac.sdc")
    bad = artifact.model_copy(update={"metadata": {
        "kind": "constraints", "format": "sdc",
        "constraints_sha256": "0" * 64
    }})
    with pytest.raises(ValueError, match="semantic hash"):
        _manifest(spec, constraints=spec.constraints.canonical_data(),
                  constraints_sha256=spec.constraints.content_sha256(),
                  constraints_artifact=bad, artifacts=[bad])
    changed = spec.constraints.model_copy(update={"clocks": [
        spec.constraints.clocks[0].model_copy(update={"period_ns": 8.0})
    ]})
    changed_artifact = artifact.model_copy(update={"metadata": {
        "kind": "constraints", "format": "sdc",
        "constraints_sha256": changed.content_sha256()
    }})
    changed_manifest = _manifest(spec, constraints=changed.canonical_data(),
                                 constraints_sha256=changed.content_sha256(),
                                 constraints_artifact=changed_artifact, artifacts=[changed_artifact])
    with pytest.raises(ValueError, match="bytes do not match"):
        repo.save_manifest(changed_manifest)

