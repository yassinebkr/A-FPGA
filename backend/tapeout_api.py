"""REST contracts for Tiny Tapeout hardening and release evidence.

The hardening endpoint uses the same JobManager and process-group lifecycle as
the agent tool.  Release-gate evaluation accepts an explicit, hashed
``TinyTapeoutRun`` record and fails closed; it never infers signoff from a
textual job result.
"""

from __future__ import annotations

import json

import jobs
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from tapeout import (
    TinyTapeoutConfig,
    detect_tiny_tapeout_capabilities,
    run_tiny_tapeout_hardening_job,
)
from tapeout_repro import (
    ReleaseGate,
    TapeoutEvidenceRepository,
    TinyTapeoutRun,
    canonical_json,
    evaluate_release_gate,
    sha256_bytes,
)


class TapeoutRequest(BaseModel):
    wrapper_verilog: str = Field(min_length=1)
    top_module: str
    title: str | None = None
    author: str = "ASIC Agent"
    description: str | None = None
    clock_hz: int = Field(default=50_000_000, gt=0)
    session: str | None = None


class ReleaseGateRequest(BaseModel):
    run: TinyTapeoutRun
    rerun: TinyTapeoutRun | None = None
    require_artifacts: bool = True


router = APIRouter(prefix="/api/tapeout", tags=["tapeout"])

# Read-only access to immutable Tiny Tapeout evidence.  This store is separate
# from the JobManager: a job result is not evidence until it is persisted.
evidence_repository = TapeoutEvidenceRepository()


def _evidence_response(run_id: str, record: dict) -> dict:
    """Return one canonical record and its verified content digest."""
    digest = sha256_bytes(canonical_json(record).encode("utf-8"))
    return {
        "id": run_id,
        "digest": digest,
        "run": record["run"],
        "release_gate": record["release_gate"],
    }


def _load_evidence(run_id: str) -> dict:
    """Load one record, mapping unsafe, corrupt, and missing IDs to 404."""
    try:
        safe_id = TapeoutEvidenceRepository._safe_run_id(run_id)
        if safe_id in {".", ".."}:
            raise ValueError("invalid Tiny Tapeout evidence run id")
        path = evidence_repository.runs / f"{safe_id}.json"
        if not path.is_file() or path.is_symlink():
            raise ValueError("evidence record is missing or symlinked")
        record = evidence_repository.load(safe_id)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="tapeout evidence run not found") from exc
    if (not isinstance(record, dict) or
            not isinstance(record.get("run"), dict) or
            not isinstance(record.get("release_gate"), dict) or
            record["run"].get("run_id") != safe_id):
        raise HTTPException(status_code=404, detail="tapeout evidence run not found")
    try:
        # Validate the complete persisted schema before returning any fields.
        # Validation is deliberately non-mutating: the response and digest are
        # derived from the original canonical JSON record.
        TinyTapeoutRun.model_validate(record["run"])
        ReleaseGate.model_validate(record["release_gate"])
        return _evidence_response(safe_id, record)
    except (TypeError, ValueError, ValidationError):
        raise HTTPException(status_code=404, detail="tapeout evidence run not found") from None


def _job_response(raw: str) -> dict:
    """Decode the agent-compatible JSON + job marker into REST fields."""
    payload, sep, marker = raw.rpartition("\njob_id: ")
    job_id = marker.strip() if sep else None
    try:
        result = json.loads(payload if sep else raw)
    except json.JSONDecodeError:
        result = {"status": "inconclusive", "ok": False, "raw": raw}
    if isinstance(result, dict):
        result = dict(result)
        if job_id:
            result["job_id"] = job_id
        return result
    return {"status": "inconclusive", "ok": False, "result": result, "job_id": job_id}


@router.get("/capabilities")
def get_tapeout_capabilities():
    """Probe local prerequisites without downloading or running hardening."""
    return detect_tiny_tapeout_capabilities(TinyTapeoutConfig()).as_dict()


@router.post("/runs", status_code=201)
async def create_tapeout_run(request: TapeoutRequest):
    raw = await run_tiny_tapeout_hardening_job(
        request.wrapper_verilog, request.top_module,
        title=request.title, author=request.author,
        description=request.description, clock_hz=request.clock_hz,
        session=request.session, manager=jobs.manager,
    )
    return _job_response(raw)


@router.get("/runs")
@router.get("/evidence")
def list_tapeout_runs():
    """List immutable Tiny Tapeout evidence records in stable ID order."""
    try:
        paths = sorted(evidence_repository.runs.glob("*.json"), key=lambda path: path.name)
    except OSError:
        paths = []
    records = []
    for path in paths:
        # Never traverse a client-supplied path while listing records.
        if not path.is_file() or path.is_symlink():
            continue
        try:
            evidence_repository._safe_run_id(path.stem)
            records.append(_load_evidence(path.stem))
        except (HTTPException, ValueError):
            continue
    return records

@router.get("/runs/{run_id}")
@router.get("/evidence/{run_id}")
def get_tapeout_run(run_id: str):
    """Return one canonical immutable record and its verified SHA-256 digest."""
    return _load_evidence(run_id)


@router.post("/release-gate", response_model=ReleaseGate)
def evaluate_tapeout_release(request: ReleaseGateRequest):
    """Evaluate explicit reproducibility evidence; no signoff is inferred."""
    return evaluate_release_gate(
        request.run, rerun=request.rerun,
        require_artifacts=request.require_artifacts,
    )


__all__ = ["ReleaseGateRequest", "TapeoutRequest", "router"]
