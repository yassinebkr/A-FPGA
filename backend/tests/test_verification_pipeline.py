"""Correct/broken RTL gate tests for the unified verification pipeline."""

import asyncio

import pytest

from evidence import DesignSpec, EvidenceRepository, Requirement, VerificationPlan
from verification_pipeline import (
    GateStatus,
    VerificationAdapters,
    VerificationGateError,
    VerificationPipeline,
    default_verification_adapters,
    require_synthesis_allowed,
    run_verification_job,
)
from verification_policy import CoverageResult


def _spec() -> DesignSpec:
    return DesignSpec(
        design_id="counter",
        name="Counter",
        top_module="counter_top",
        requirements=[Requirement(id="count", text="counter advances")],
        verification=VerificationPlan(
            design_class="sequential",
            assertions=[{
                "id": "no_x", "name": "no_x", "expression": "1'b1",
                "requirement_id": "count",
            }],
            coverage_requirements=[{
                "id": "count", "target": "inputs.value", "field": "inputs.value",
                "bins": [1, 2], "threshold": 1.0,
                "requirement_id": "count",
            }],
        ),
    )


def _adapters(*, broken=False):
    return VerificationAdapters(
        lint=lambda **_: {
            "ok": not broken,
            "errors": ([{"code": "ExpectedToken", "severity": "error",
                          "msg": "syntax error"}] if broken else []),
            "tool": "mock-lint",
        },
        differential=lambda **_: {"status": "failed" if broken else "passed"},
        assertions=lambda **_: [{"property_id": "no_x", "status": "proven",
                                 "requirement_id": "count"}],
        functional_coverage=lambda **_: [CoverageResult.from_counts(
            "count", 5 if broken else 95, 100, 0.9)],
        structural_coverage=lambda **_: {"status": "passed"},
    )


def test_correct_rtl_produces_manifest_matrix_and_delivery_gate():
    async def run():
        pipeline = VerificationPipeline(_adapters())
        result = await pipeline.run(_spec(), "module counter_top; endmodule")
        assert result.gate.allowed is True
        assert result.gate.status is GateStatus.PASSED
        assert result.manifest.status == "completed"
        assert result.report.overall_status == "passed"
        assert result.matrix[0].status == "passed"
        require_synthesis_allowed(result)

    asyncio.run(run())


def test_broken_rtl_blocks_synthesis_and_reports_failed_requirement():
    async def run():
        pipeline = VerificationPipeline(_adapters(broken=True))
        result = await pipeline.run(_spec(), "module counter_top broken")
        assert result.gate.allowed is False
        assert result.gate.status is GateStatus.FAILED
        assert "lint" in result.gate.blocking_stages
        assert result.matrix[0].status == "failed"
        with pytest.raises(VerificationGateError):
            require_synthesis_allowed(result)

    asyncio.run(run())


def test_missing_core_adapter_is_inconclusive_and_never_a_pass():
    async def run():
        pipeline = VerificationPipeline(VerificationAdapters(
            lint=lambda **_: {"ok": True, "errors": []},
            differential=lambda **_: {"status": "passed"},
        ))
        result = await pipeline.run(_spec(), "module counter_top; endmodule")
        assert result.gate.allowed is False
        assert result.gate.status is GateStatus.INCONCLUSIVE
        assert "assertions" in result.gate.blocking_stages

    asyncio.run(run())


def test_missing_required_coverage_is_not_hidden_by_differential_pass():
    async def run():
        adapters = _adapters()
        adapters.functional_coverage = lambda **_: []
        result = await VerificationPipeline(adapters).run(
            _spec(), "module counter_top; endmodule")
        assert result.gate.status is GateStatus.INCONCLUSIVE
        assert result.matrix[0].status == "inconclusive"
        assert "functional_coverage" in result.gate.blocking_stages

    asyncio.run(run())


def test_later_adapters_receive_prior_stage_results_and_vector_coverage(tmp_path):
    async def run():
        adapters = default_verification_adapters()
        result = await adapters.functional_coverage(
            spec=_spec(),
            raw_results={"differential_regression": {"samples": [
                {"vector_id": "v0", "inputs": {"value": 1},
                 "expected": {"value": 2}, "actual": {"value": 2}, "passed": True},
                {"vector_id": "v1", "inputs": {"value": 2},
                 "expected": {"value": 3}, "actual": {"value": 3}, "passed": True},
            ]}},
        )
        assert result["events_sampled"] == 2
        assert result["backend"] == "cocotb-coverage"
        assert result["status"] == "passed"

        seen = {}
        custom = VerificationAdapters(
            lint=lambda **_: {"ok": True, "errors": []},
            differential=lambda **_: {"status": "passed"},
            assertions=lambda **_: [{"property_id": "no_x", "status": "proven",
                                     "requirement_id": "count"}],
            functional_coverage=lambda **kwargs: (
                seen.update(kwargs) or [CoverageResult.from_counts("count", 1, 1, .9)]
            ),
            structural_coverage=lambda **_: {"status": "passed"},
        )
        pipeline_result = await VerificationPipeline(custom).run(
            _spec(), "module counter_top; endmodule")
        assert seen["stage_results"][0].name == "lint"
        assert seen["raw_results"]["differential_regression"]["status"] == "passed"
        assert pipeline_result.gate.allowed

    asyncio.run(run())



def test_wave3_mandatory_stages_block_and_reference_is_propagated():
    async def run():
        spec = _spec().model_copy(update={"verification": _spec().verification.model_copy(update={
            "requires_formal": True, "requires_equivalence": True, "requires_cdc_rdc": True,
        })})
        adapters = _adapters()
        seen = {}
        adapters.formal_properties = lambda **_: {"status": "bounded"}
        adapters.equivalence = lambda **kwargs: (
            seen.update(kwargs) or {"status": "proven"}
        )
        adapters.cdc_rdc = lambda **_: {"status": "pass"}
        result = await VerificationPipeline(adapters).run(
            spec, "module counter_top; endmodule",
            reference_rtl_code="module counter_top; endmodule",
        )
        assert result.gate.status is GateStatus.INCONCLUSIVE
        assert "formal_properties" in result.gate.blocking_stages
        assert seen["reference_rtl_code"].startswith("module counter_top")
        assert next(stage for stage in result.manifest.stages
                    if stage.name == "equivalence").status == "passed"
        assert next(stage for stage in result.manifest.stages
                    if stage.name == "cdc_rdc").status == "passed"

        adapters.cdc_rdc = lambda **_: {"status": "fail"}
        failed = await VerificationPipeline(adapters).run(
            spec, "module counter_top; endmodule",
            reference_rtl_code="module counter_top; endmodule",
        )
        assert failed.gate.status is GateStatus.FAILED
        assert "cdc_rdc" in failed.gate.blocking_stages

    asyncio.run(run())


def test_pipeline_persists_manifest_report_and_artifacts(tmp_path):
    async def run():
        repository = EvidenceRepository(tmp_path / "evidence")
        pipeline = VerificationPipeline(_adapters(), evidence_repository=repository)
        result = await pipeline.run(_spec(), "module counter_top; endmodule", persist=True)
        assert repository.load_manifest(result.manifest.run_id) == result.manifest
        assert repository.load_report(result.manifest.run_id) == result.report
        assert result.manifest.artifacts
        assert all(item.artifact_hashes for item in result.manifest.stages)

    asyncio.run(run())


def test_job_manager_runner_publishes_structured_manifest(tmp_path):
    async def run():
        from jobs import JobManager

        manager = JobManager(db_path=str(tmp_path / "jobs.db"),
                             jobs_root=str(tmp_path / "jobs"))
        try:
            pipeline = VerificationPipeline(_adapters(),
                                            evidence_repository=EvidenceRepository(tmp_path / "evidence"))
            result = await run_verification_job(manager, pipeline, _spec(),
                                                "module counter_top; endmodule",
                                                session="proj-test", persist=True)
            assert result.gate.allowed
            job = next(iter(manager.jobs.values()))
            assert job.kind == "verification"
            assert job.outcome == "success"
            assert job.manifest["verification"]["gate"]["allowed"] is True
        finally:
            await manager.stop()

    asyncio.run(run())


def test_requires_hardware_enforces_injected_board_neutral_adapter():
    async def run():
        base = _spec()
        spec = base.model_copy(update={"verification": base.verification.model_copy(update={"requires_hardware": True})})
        adapters = _adapters()
        seen = {}
        adapters.hardware = lambda **kwargs: (seen.update(kwargs) or {"status": "passed", "available": True, "backend": "mock-hil"})
        result = await VerificationPipeline(adapters).run(spec, "module counter_top; endmodule")
        stage = next(item for item in result.manifest.stages if item.name == "hardware")
        assert stage.mandatory is True
        assert stage.status == "passed"
        assert seen["top_module"] == "counter_top"
        assert result.gate.allowed is True
    asyncio.run(run())


def test_mandatory_hardware_requirement_auto_enforces_unavailable_stage():
    async def run():
        base = _spec()
        spec = base.model_copy(update={"requirements": [
            Requirement(id="count", text="counter advances"),
            Requirement(id="board", text="runs on hardware", kind="hardware"),
        ]})
        result = await VerificationPipeline(_adapters()).run(
            spec, "module counter_top; endmodule")
        stage = next(item for item in result.manifest.stages if item.name == "hardware")
        assert stage.mandatory is True
        assert stage.status == "inconclusive"
        assert "hardware" in result.gate.blocking_stages
        board_result = next(item for item in result.matrix if item.requirement_id == "board")
        assert board_result.status == "inconclusive"
        assert board_result.evidence == ["hardware"]
    asyncio.run(run())


def test_default_hardware_adapter_is_conservative():
    async def run():
        adapter = default_verification_adapters().hardware
        result = await adapter()
        assert result["status"] == "inconclusive"
        assert result["available"] is False
    asyncio.run(run())
