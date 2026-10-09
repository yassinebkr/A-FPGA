"""Structured REST entry point for the unified verification job."""

from __future__ import annotations

import jobs
from constraints import ConstraintSet
from evidence import DesignSpec, repository as default_repository
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from verification_pipeline import VerificationPipeline, run_verification_job


class VerificationRequest(BaseModel):
    verilog_code: str = Field(min_length=1)
    testbench_code: str = ""
    reference_rtl_code: str | None = None
    top_module: str
    design_spec: DesignSpec | None = None
    # Optional shorthand for clients that only need to provide expert timing
    # constraints; a full DesignSpec remains the authoritative form.
    constraints: ConstraintSet | None = None
    target: str | None = None
    run_icarus: bool = False
    session: str | None = None


router = APIRouter(prefix="/api/verification", tags=["verification"])


@router.post("/runs", status_code=201)
async def create_verification_run(request: VerificationRequest):
    if request.design_spec is None:
        spec = DesignSpec(design_id=request.top_module, name=request.top_module,
                          top_module=request.top_module,
                          constraints=request.constraints or ConstraintSet())
    else:
        spec = request.design_spec
        if request.constraints is not None and request.constraints != spec.constraints:
            raise HTTPException(status_code=422,
                                detail="constraints must match design_spec.constraints")
    if spec.top_module != request.top_module:
        raise HTTPException(status_code=422,
                            detail="design_spec.top_module must match top_module")
    pipeline = VerificationPipeline(
        evidence_repository=default_repository,
        manager=jobs.manager,
        run_portability=request.run_icarus,
    )
    result = await run_verification_job(
        jobs.manager, pipeline, spec, request.verilog_code,
        testbench_code=request.testbench_code, target=request.target,
        reference_rtl_code=request.reference_rtl_code,
        session=request.session, persist=True,
    )
    return result.as_dict()


@router.get("/runs/{run_id}")
def get_verification_run(run_id: str):
    manifest = default_repository.load_manifest(run_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="verification run not found")
    report = default_repository.load_report(run_id)
    gate = manifest.metadata.get("verification_gate", {})
    return {"manifest": manifest, "report": report, "gate": gate}


__all__ = ["VerificationRequest", "router"]
