"""Small REST surface for the evidence records.

The existing chat/job APIs remain unchanged.  These endpoints expose the
versioned records for tooling and the future expert UI; they do not infer a
manufacturing result from a job's textual output.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from evidence import (DesignSpec, EvidenceError, EvidenceReport, ImmutableRunError, RunManifest,
                    build_evidence_report, repository, sha256_record)


router = APIRouter(prefix="/api/evidence", tags=["evidence"])

@router.post("/specs", status_code=201)
def save_spec(spec: DesignSpec):
    try:
        digest = repository.save_spec(spec)
    except (EvidenceError, ImmutableRunError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"design_id": spec.design_id, "revision": spec.revision,
            "spec_sha256": digest,
            "constraints_sha256": spec.constraints.content_sha256()}


@router.get("/specs/{design_id}/{revision}")
def get_spec(design_id: str, revision: int):
    spec = repository.load_spec(design_id, revision)
    if spec is None:
        raise HTTPException(status_code=404, detail="specification not found")
    return spec


@router.get("/specs/{design_id}/{revision}/constraints")
def get_spec_constraints(design_id: str, revision: int):
    "Return the typed constraint contract and its content hash."
    spec = repository.load_spec(design_id, revision)
    if spec is None:
        raise HTTPException(status_code=404, detail="specification not found")
    return {"design_id": design_id, "revision": revision,
            "constraints": spec.constraints,
            "constraints_sha256": spec.constraints.content_sha256()}

@router.post("/runs", status_code=201)
def save_run(manifest: RunManifest):
    spec = repository.load_spec(manifest.design_id, manifest.design_revision)
    if spec is None:
        raise HTTPException(status_code=404, detail="referenced specification not found")
    if spec.design_id != manifest.design_id or spec.revision != manifest.design_revision:
        raise HTTPException(status_code=409, detail="manifest/spec identity mismatch")
    if manifest.constraints_artifact is not None:
        if manifest.constraints_sha256 != spec.constraints.content_sha256():
            raise HTTPException(status_code=409, detail="manifest constraints_sha256 does not match stored specification")
        if manifest.constraints != spec.constraints.canonical_data():
            raise HTTPException(status_code=409, detail="manifest constraints do not match stored specification")
    if sha256_record(spec) != manifest.spec_sha256:
        raise HTTPException(status_code=409, detail="manifest spec_sha256 does not match stored specification")
    if manifest.parent_run_id and repository.load_manifest(manifest.parent_run_id) is None:
        raise HTTPException(status_code=404, detail="parent run not found")
    try:
        digest = repository.save_manifest(manifest)
    except (EvidenceError, ImmutableRunError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    report = build_evidence_report(manifest, spec)
    repository.save_report(report)
    return {"run_id": manifest.run_id, "manifest_sha256": digest,
            "report": report}

@router.get("/runs")
def list_runs():
    return list(repository.iter_manifests())


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    manifest = repository.load_manifest(run_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="run manifest not found")
    report = repository.load_report(run_id)
    return {"manifest": manifest, "report": report}


@router.get("/runs/{run_id}/report")
def get_report(run_id: str):
    report = repository.load_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="evidence report not found")
    return report


@router.get("/runs/{run_id}/assurance")
def get_assurance(run_id: str):
    """Return the conservative provenance class and its rationale."""
    report = repository.load_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="evidence report not found")
    return {"run_id": run_id, "assurance": report.assurance,
            "evidence_classes": report.evidence_classes,
            "basis": report.assurance_basis,
            "limitations": report.limitations}


@router.get("/runs/{run_id}/implementation")
def get_implementation_report(run_id: str):
    report = repository.load_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="evidence report not found")
    return report.implementation

