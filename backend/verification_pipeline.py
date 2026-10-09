"""Unified verification orchestration and delivery gate.

The pipeline is deliberately an adapter boundary.  The repository has several
different execution engines (lint, cocotb/golden-model regression, generated
SVA, and coverage tools), and none of them should become the authority for the
others.  ``VerificationAdapters`` lets those engines be supplied independently
while this module owns ordering, conservative gate semantics, evidence, and
the JobManager-facing lifecycle.

An adapter may be synchronous or asynchronous and may return a mapping, a
dataclass, or one of the project evidence models.  The normalized result is a
``VerificationRun`` containing a requirement matrix, a versioned
``RunManifest`` and the derived ``EvidenceReport``.  A failed *or
inconclusive* mandatory stage never authorizes synthesis or delivery.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping, Protocol

from evidence import (
    ArtifactRef,
    DesignSpec,
    EvidenceReport,
    EvidenceRepository,
    RequirementResult,
    RunManifest,
    StageResult,
    build_evidence_report,
    canonical_json,
    repository as default_repository,
    sha256_bytes,
    sha256_record,
)
from verification_policy import (
    AssertionResult,
    CoverageResult,
    LintPolicy,
    apply_lint_policy,
    assertions_from_design_spec,
    build_verification_evidence,
    coverage_requirements_from_design_spec,
    lint_policy_from_design_spec,
)

logger = logging.getLogger("verification")


class VerificationGateError(RuntimeError):
    """Raised when a caller attempts synthesis/delivery without signoff."""

    def __init__(self, gate: "VerificationGate"):
        self.gate = gate
        super().__init__(gate.reason)


class GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class VerificationGate:
    """Machine-readable synthesis/delivery decision."""

    allowed: bool
    status: GateStatus
    blocking_stages: tuple[str, ...] = ()
    blocking_requirements: tuple[str, ...] = ()
    reason: str = ""

    def require(self) -> None:
        if not self.allowed:
            raise VerificationGateError(self)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "status": self.status.value,
            "blocking_stages": list(self.blocking_stages),
            "blocking_requirements": list(self.blocking_requirements),
            "reason": self.reason,
        }


class StageName(StrEnum):
    LINT = "lint"
    DIFFERENTIAL = "differential_regression"
    ASSERTIONS = "assertions"
    FUNCTIONAL_COVERAGE = "functional_coverage"
    STRUCTURAL_COVERAGE = "structural_coverage"
    FORMAL_PROPERTIES = "formal_properties"
    EQUIVALENCE = "equivalence"
    CDC_RDC = "cdc_rdc"
    HARDWARE = "hardware"
    PORTABILITY = "portability_smoke"


class Adapter(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


@dataclass
class VerificationAdapters:
    """Execution hooks injected by concrete runner implementations.

    ``portability`` is intentionally optional: an Icarus smoke suite is a
    useful cross-check, but lack of Icarus must not make a Verilator-only
    deployment claim that the design is unverified.  The five core adapters
    are mandatory by default and an absent core adapter produces an
    *inconclusive* gate, never a false pass.
    """

    lint: Adapter | None = None
    differential: Adapter | None = None
    assertions: Adapter | None = None
    functional_coverage: Adapter | None = None
    structural_coverage: Adapter | None = None
    formal_properties: Adapter | None = None
    equivalence: Adapter | None = None
    cdc_rdc: Adapter | None = None
    hardware: Adapter | None = None
    portability: Adapter | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, Adapter]) -> "VerificationAdapters":
        allowed = {
            "lint", "differential", "assertions", "functional_coverage",
            "structural_coverage", "formal_properties", "equivalence",
            "cdc_rdc", "hardware", "portability",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("unknown verification adapter(s): " + ", ".join(sorted(unknown)))
        return cls(**dict(values))


def default_verification_adapters(*, manager: Any | None = None,
                                  repository: EvidenceRepository | None = None) -> VerificationAdapters:
    """Build adapters backed by the project's concrete open-source tools.

    Imports are intentionally lazy: importing the pipeline must remain safe in
    minimal environments (for example the frontend mock server) where EDA
    binaries or optional cocotb dependencies are absent.  Each adapter still
    returns a conservative inconclusive result when its executable is missing.
    """
    async def lint(*, verilog_code: str, top_module: str, policy: Any = None, **_: Any):
        from eda_tools import write_and_check_syntax
        try:
            value = await asyncio.to_thread(write_and_check_syntax, verilog_code, top_module, policy)
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return {"status": "inconclusive", "ok": False, "reason": str(value)}
        except Exception as exc:
            return {"status": "failed", "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def differential(*, verilog_code: str, top_module: str, spec: Any,
                           session: str | None = None, **kwargs: Any):
        from differential_regression import (
            DifferentialRunConfig, run_differential_regression_report,
        )
        metadata = getattr(spec, "metadata", {}) or {}
        verification = getattr(spec, "verification", None)
        model = metadata.get("reference_model") or metadata.get("golden_model")
        if model is None and isinstance(verification, Mapping):
            model = verification.get("reference_model")
        if not model:
            return {"status": "inconclusive",
                    "reason": "DesignSpec.metadata.reference_model is required for differential regression"}
        config = DifferentialRunConfig(
            top_module, verilog_code, str(model),
            Path(__file__).with_name("regression_corpus"),
            run_icarus=bool(kwargs.get("run_icarus", False)), session=session,
        )
        return await run_differential_regression_report(
            config, manager=manager or kwargs.get("manager"))

    async def assertions(*, verilog_code: str, testbench_code: str,
                         top_module: str, spec: Any, **kwargs: Any):
        from verification_executor import VerilatorAssertionExecutor
        executor = VerilatorAssertionExecutor()
        return await asyncio.to_thread(
            executor.execute, verilog_code, testbench_code, top_module, spec,
            repository=repository or kwargs.get("repository"),
        )

    async def functional_coverage(*, spec: Any, raw_results: Mapping[str, Any] | None = None,
                                  repository: Any = None, **kwargs: Any):
        # Functional coverage consumes *actual* differential samples.  A
        # corpus vector alone is not evidence: expected/actual must be
        # observed by the simulator worker first.
        from verification_executor import FunctionalCoverageAdapter
        collector = FunctionalCoverageAdapter.from_design_spec(spec)
        differential = _as_mapping((raw_results or {}).get(StageName.DIFFERENTIAL.value, {}))
        samples = differential.get("samples", [])
        collector.sample_events(item for item in samples if isinstance(item, Mapping))
        run_dir = _as_mapping(differential.get("artifacts", {})).get("run_dir")
        export_root = kwargs.get("coverage_root")
        if export_root is None and run_dir:
            export_root = Path(str(run_dir)) / "functional_coverage"
        artifacts = collector.export(export_root, repository=repository) if export_root else {"paths": [], "artifacts": []}
        status = collector.status() if samples else "inconclusive"
        return {"status": status, "ok": status == "passed", "backend": collector.backend,
                "events_sampled": collector._events_sampled,
                "reason": None if samples else "differential regression produced no sampled events",
                "coverage": [item.model_dump(mode="json") for item in collector.results()],
                "artifacts": artifacts.get("artifacts", []), "raw_paths": artifacts.get("paths", [])}

    async def structural_coverage(*, verilog_code: str, testbench_code: str,
                                  top_module: str, **kwargs: Any):
        from verification_executor import VerilatorCoverageExecutor
        executor = VerilatorCoverageExecutor()
        return await asyncio.to_thread(
            executor.collect, verilog_code, testbench_code, top_module,
            repository=repository or kwargs.get("repository"),
        )

    async def formal_properties(*, verilog_code: str, top_module: str, spec: Any, **kwargs: Any):
        from formal_executor import FormalExecutor
        verification = spec.verification
        executor = FormalExecutor(repository=repository or kwargs.get("repository"))
        return await asyncio.to_thread(
            executor.execute, verilog_code, top_module, spec,
            repository=repository or kwargs.get("repository"),
            depth=verification.formal_depth, mode=verification.formal_mode,
        )

    async def equivalence(*, verilog_code: str, reference_rtl_code: str | None,
                          top_module: str, **kwargs: Any):
        if not reference_rtl_code:
            return {"status": "inconclusive", "ok": False,
                    "reason": "reference RTL is required for equivalence"}
        from formal_executor import FormalExecutor
        executor = FormalExecutor(repository=repository or kwargs.get("repository"))
        return await asyncio.to_thread(
            executor.equivalence, reference_rtl_code, verilog_code, top_module,
            repository=repository or kwargs.get("repository"),
        )

    async def cdc_rdc(*, verilog_code: str, top_module: str, spec: Any, **kwargs: Any):
        from cdc_rdc import analyze_cdc_rdc
        report = await asyncio.to_thread(
            analyze_cdc_rdc, verilog_code=verilog_code, top_module=top_module,
            waivers=spec.verification.cdc_waivers,
        )
        return report.as_dict()

    async def hardware(**_: Any):
        """Board-neutral hardware adapter placeholder.

        A real FPGA/HIL harness is injected through ``VerificationAdapters``.
        Until then, hardware evidence is explicitly inconclusive rather than
        being inferred from simulation or synthesis.
        """
        return {
            "status": "inconclusive",
            "available": False,
            "reason": "no board-neutral hardware adapter is configured",
        }

    async def portability(*, raw_results: Mapping[str, Any] | None = None, **_: Any):
        portability_report = _as_mapping(
            _as_mapping((raw_results or {}).get(StageName.DIFFERENTIAL.value, {})).get("portability"))
        if not portability_report:
            return {"status": "not_applicable",
                    "reason": "Icarus portability was not requested"}
        status = str(portability_report.get("status", "inconclusive"))
        if status in {"passed", "success"}:
            return {"status": "passed", "details": portability_report}
        if status in {"skipped", "not_applicable"}:
            return {"status": "not_applicable", "details": portability_report}
        return {"status": "failed" if status in {"compile_failure", "runtime_failure", "mismatch", "failed"}
                else "inconclusive", "details": portability_report}

    return VerificationAdapters(
        lint=lint, differential=differential, assertions=assertions,
        functional_coverage=functional_coverage,
        structural_coverage=structural_coverage,
        formal_properties=formal_properties, equivalence=equivalence,
        cdc_rdc=cdc_rdc, hardware=hardware, portability=portability,
    )


@dataclass(frozen=True)
class VerificationRun:
    """Pipeline output; this is also the structured job/API payload."""

    manifest: RunManifest
    report: EvidenceReport
    matrix: tuple[RequirementResult, ...]
    gate: VerificationGate
    raw_results: Mapping[str, Any] = field(default_factory=dict, repr=False)
    job_id: str | None = None

    @property
    def passed(self) -> bool:
        return self.gate.allowed

    def as_dict(self) -> dict[str, Any]:
        dump = lambda value: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        return {
            "manifest": dump(self.manifest),
            "report": dump(self.report),
            "requirement_matrix": [dump(item) for item in self.matrix],
            "gate": self.gate.as_dict(),
            "job_id": self.job_id,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _dump(value.model_dump(mode="json"))
    if hasattr(value, "value") and not isinstance(value, (str, bytes, dict)):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    return value


async def _invoke(adapter: Adapter, **kwargs: Any) -> Any:
    """Call an adapter without forcing every runner to be async."""
    # Small adapters in tests and in tool wrappers often declare only the
    # arguments they consume.  Filter the shared context unless the adapter
    # explicitly accepts **kwargs; this keeps the dependency boundary loose
    # without catching TypeErrors raised *inside* an adapter.
    try:
        signature = inspect.signature(adapter)
        accepts_kwargs = any(item.kind is inspect.Parameter.VAR_KEYWORD
                             for item in signature.parameters.values())
        if not accepts_kwargs:
            kwargs = {key: value for key, value in kwargs.items()
                      if key in signature.parameters}
    except (TypeError, ValueError):
        pass
    result = adapter(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    dumped = _dump(value)
    return dumped if isinstance(dumped, Mapping) else {}


def _bool_result(value: Any, *, keys=("ok", "passed", "success")) -> bool | None:
    if isinstance(value, bool):
        return value
    mapping = _as_mapping(value)
    for key in keys:
        if key in mapping:
            return bool(mapping[key])
    status = mapping.get("status", getattr(value, "status", None))
    if hasattr(status, "value"):
        status = status.value
    if status is not None:
        if str(status).lower() in {"passed", "pass", "success", "proven", "not-applicable"}:
            return True
        if str(status).lower() in {"failed", "fail", "failure", "dut_failure", "reference_failure"}:
            return False
    return None


def _stage_status(value: Any, *, unavailable: bool = False) -> str:
    if unavailable:
        return "inconclusive"
    mapping = _as_mapping(value)
    status = mapping.get("status", getattr(value, "status", None))
    if hasattr(status, "value"):
        status = status.value
    if status is not None:
        status = str(status).lower()
        if status in {"passed", "pass", "success", "proven", "not-applicable", "not_applicable"}:
            return "passed" if status != "not-applicable" and status != "not_applicable" else "not_applicable"
        if status in {"failed", "fail", "failure", "dut_failure", "reference_failure"}:
            return "failed"
        if status in {"skipped", "not_applicable"}:
            return "not_applicable"
        if status in {"inconclusive", "bounded", "pending", "running"}:
            return "inconclusive"
    verdict = str(mapping.get("verdict", "")).lower()
    if verdict in {"ok", "pass", "passed"}:
        return "passed"
    if verdict in {"syntax_error", "elaboration_error", "failed", "failure"}:
        return "failed"
    measured = mapping.get("percentage", mapping.get("coverage", mapping.get("coverage_ratio")))
    threshold = mapping.get("threshold")
    if isinstance(measured, (int, float)) and isinstance(threshold, (int, float)):
        return "passed" if measured >= threshold else "failed"
    if isinstance(mapping.get("covered"), (int, float)) and isinstance(mapping.get("total"), (int, float)):
        ratio = mapping["covered"] / mapping["total"] if mapping["total"] else 0.0
        return "passed" if threshold is not None and ratio >= threshold else "inconclusive"
    result = _bool_result(value)
    if result is True:
        return "passed"
    if result is False:
        return "failed"
    return "inconclusive"


def _result_list(value: Any, key: str) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    mapping = _as_mapping(value)
    raw = mapping.get(key, mapping.get("results", []))
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    return list(raw) if isinstance(raw, (list, tuple)) else []


class VerificationPipeline:
    """Run the ordered verification stages and derive durable evidence."""

    CORE_STAGES = (
        StageName.LINT,
        StageName.DIFFERENTIAL,
        StageName.ASSERTIONS,
        StageName.FUNCTIONAL_COVERAGE,
        StageName.STRUCTURAL_COVERAGE,
        StageName.FORMAL_PROPERTIES,
        StageName.EQUIVALENCE,
        StageName.CDC_RDC,
        StageName.HARDWARE,
    )

    def __init__(self, adapters: VerificationAdapters | Mapping[str, Adapter] | None = None, *,
                 evidence_repository: EvidenceRepository | None = None,
                 require_structural_coverage: bool = True,
                 run_portability: bool = False, manager: Any | None = None):
        self.manager = manager
        self.repository = evidence_repository
        self._default_adapters = adapters is None
        self.adapters = (adapters if isinstance(adapters, VerificationAdapters)
                         else VerificationAdapters.from_mapping(adapters)
                         if adapters is not None else
                         default_verification_adapters(manager=manager, repository=evidence_repository))
        self.require_structural_coverage = require_structural_coverage
        self.run_portability = run_portability

    async def run(self, spec: DesignSpec, rtl_code: str, *,
                  testbench_code: str = "", target: str | None = None,
                  reference_rtl_code: str | None = None,
                  parent_run_id: str | None = None, session: str | None = None,
                  run_id: str | None = None, persist: bool = False,
                  job: Any | None = None) -> VerificationRun:
        """Execute stages in order and return the conservative signoff result."""
        if persist and self.repository is None:
            self.repository = default_repository
        if persist:
            self.repository.save_spec(spec)
        policy = lint_policy_from_design_spec(spec)
        context = {
            "spec": spec, "rtl_code": rtl_code, "testbench_code": testbench_code,
            "verilog_code": rtl_code, "code": rtl_code, "top_module": spec.top_module,
            "policy": policy, "session": session, "repository": self.repository,
            "manager": self.manager, "run_icarus": self.run_portability,
            "reference_rtl_code": reference_rtl_code,
        }
        stages: list[StageResult] = []
        raw_results: dict[str, Any] = {}
        requirement_links: dict[str, list[tuple[str, str]]] = {}

        async def emit(message: str) -> None:
            logger.info("%s", message)
            if job is not None and hasattr(job, "emit"):
                await job.emit(message)

        await emit("verification: starting ordered gate pipeline")
        for name in self.CORE_STAGES:
            required = name != StageName.STRUCTURAL_COVERAGE or self.require_structural_coverage
            if name is StageName.ASSERTIONS:
                required = bool(assertions_from_design_spec(spec))
            elif name is StageName.FORMAL_PROPERTIES:
                required = bool(spec.verification.requires_formal)
            elif name is StageName.EQUIVALENCE:
                required = bool(spec.verification.requires_equivalence)
            elif name is StageName.CDC_RDC:
                reset_ports = {item.reset_port for item in spec.clock_domains if item.reset_port}
                required = bool(spec.verification.requires_cdc_rdc or
                                len(spec.clock_domains) > 1 or len(reset_ports) > 1)
            elif name is StageName.HARDWARE:
                required = bool(spec.verification.requires_hardware or any(
                    item.kind == "hardware" and item.mandatory
                    for item in spec.requirements))
            adapter = getattr(self.adapters, name.name.lower())
            # Enum.name for DIFFERENTIAL is DIFFERENTIAL; adapter is named
            # ``differential`` rather than ``differential_regression``.
            if name is StageName.FUNCTIONAL_COVERAGE:
                adapter = self.adapters.functional_coverage
            elif name is StageName.STRUCTURAL_COVERAGE:
                adapter = self.adapters.structural_coverage
            elif name is StageName.ASSERTIONS:
                adapter = self.adapters.assertions
            elif name is StageName.DIFFERENTIAL:
                adapter = self.adapters.differential
            elif name is StageName.LINT:
                adapter = self.adapters.lint
            elif name is StageName.FORMAL_PROPERTIES:
                adapter = self.adapters.formal_properties
            elif name is StageName.EQUIVALENCE:
                adapter = self.adapters.equivalence
            elif name is StageName.CDC_RDC:
                adapter = self.adapters.cdc_rdc
            elif name is StageName.HARDWARE:
                adapter = self.adapters.hardware
            stage_context = dict(context)
            stage_context["stage_results"] = tuple(stages)
            stage_context["raw_results"] = dict(raw_results)
            result, stage = await self._run_stage(name, adapter, stage_context, policy, required, emit)
            raw_results[name.value] = result
            stages.append(stage)
            self._collect_links(name, result, requirement_links)

        if self.run_portability:
            stage_context = dict(context)
            stage_context["stage_results"] = tuple(stages)
            stage_context["raw_results"] = dict(raw_results)
            result, stage = await self._run_stage(
                StageName.PORTABILITY, self.adapters.portability, stage_context, policy,
                False, emit)
            raw_results[StageName.PORTABILITY.value] = result
            stages.append(stage)

        matrix = self._requirement_matrix(spec, stages, raw_results, requirement_links)
        gate = self._gate(stages, matrix)
        artifact_refs = []
        if persist and self.repository is not None:
            # Promote content-addressed artifacts emitted by concrete stages
            # (formal traces, equivalence logs, coverage reports) into the run
            # manifest so evidence consumers can resolve them directly.
            declared_hashes = set()
            for raw in raw_results.values():
                for artifact in _as_mapping(raw).get("artifacts", []):
                    if not isinstance(artifact, Mapping) or not artifact.get("sha256"):
                        continue
                    try:
                        ref = ArtifactRef.model_validate(artifact)
                    except (TypeError, ValueError):
                        continue
                    if ref.sha256 not in declared_hashes:
                        artifact_refs.append(ref)
                        declared_hashes.add(ref.sha256)
            # Store the exact source and normalized stage records as evidence.
            for name, content, media_type in (
                (f"{spec.top_module}.v", rtl_code.encode(), "text/plain"),
                (f"{spec.top_module}.tb.v", testbench_code.encode(), "text/plain"),
                ("verification-results.json", canonical_json({
                    "stages": [_dump(item) for item in stages],
                    "matrix": [_dump(item) for item in matrix],
                    "gate": gate.as_dict(),
                }).encode(), "application/json"),
            ):
                artifact_refs.append(self.repository.put_artifact(content, name=name, media_type=media_type))
            source_hash = {item.name: item.sha256 for item in artifact_refs}
            stages = [item.model_copy(update={
                "artifact_hashes": [source_hash["verification-results.json"]]
            }) for item in stages]

        manifest = RunManifest(
            run_id=run_id or f"run-verification-{sha256_record({'design_id': spec.design_id, 'rtl_sha256': sha256_bytes(rtl_code.encode()), 'created_at': _now().isoformat()})[:16]}",
            design_id=spec.design_id,
            design_revision=spec.revision,
            spec_sha256=sha256_record(spec),
            parent_run_id=parent_run_id,
            status=("completed" if gate.status is GateStatus.PASSED else gate.status.value),
            completed_at=_now(),
            target=target or spec.target,
            rtl_sha256=sha256_bytes(rtl_code.encode()),
            testbench_sha256=sha256_bytes(testbench_code.encode()) if testbench_code else None,
            constraints_sha256=spec.constraints.content_sha256(),
            constraints=spec.constraints.canonical_data(),
            stages=stages,
            requirement_results=list(matrix),
            artifacts=artifact_refs,
            warnings=list(gate.blocking_stages) if not gate.allowed else [],
            metadata={"verification_gate": gate.as_dict(), "session": session,
                      "pipeline": "verification/v1"},
        )
        if persist and self.repository is not None:
            self.repository.save_manifest(manifest)
        report = build_evidence_report(manifest, spec)
        if persist and self.repository is not None:
            self.repository.save_report(report)
        outcome = "success" if gate.status is GateStatus.PASSED else gate.status.value
        if job is not None:
            job.set_outcome(outcome)
            job.manifest = {"verification": VerificationRun(
                manifest, report, tuple(matrix), gate, raw_results).as_dict()}
        await emit(f"verification: {gate.status.value}; synthesis_allowed={gate.allowed}")
        return VerificationRun(manifest, report, tuple(matrix), gate, raw_results)

    async def _run_stage(self, name: StageName, adapter: Adapter | None,
                         context: Mapping[str, Any], policy: LintPolicy,
                         mandatory: bool, emit: Callable[[str], Awaitable[None]]):
        started = _now()
        await emit(f"verification: stage {name.value} started")
        if adapter is None:
            result: Any = {"status": "inconclusive", "reason": "adapter unavailable"}
            status = "inconclusive" if mandatory else "not_applicable"
            stage = StageResult(name=name.value, status=status, mandatory=mandatory,
                                details=dict(result))
            await emit(f"verification: stage {name.value} {status} (adapter unavailable)")
            return result, stage
        try:
            result = await _invoke(adapter, **context)
            if name is StageName.LINT and isinstance(result, Mapping):
                # Preserve legacy lint JSON while making policy ownership
                # explicit for the gate.
                result = apply_lint_policy(result, policy, top_module=context["top_module"])
            if name in {StageName.ASSERTIONS, StageName.FUNCTIONAL_COVERAGE}:
                items = (list(result) if isinstance(result, (list, tuple)) else
                         _result_list(result, "assertions" if name is StageName.ASSERTIONS else "coverage"))
            else:
                items = []
            if items:
                statuses = [_stage_status(item) for item in items]
                status = ("failed" if "failed" in statuses else
                          "inconclusive" if any(item == "inconclusive" for item in statuses) else
                          "passed" if statuses else "inconclusive")
            else:
                status = _stage_status(result)
            if name is StageName.FUNCTIONAL_COVERAGE:
                required_coverage = {
                    item.requirement_id or item.id
                    for item in coverage_requirements_from_design_spec(context["spec"])
                    if item.mandatory
                }
                observed_coverage = {
                    str(_as_mapping(item).get("requirement_id", _as_mapping(item).get("id", "")))
                    for item in items
                }
                missing_coverage = sorted(required_coverage - observed_coverage)
                if missing_coverage and status == "passed":
                    status = "inconclusive"
                if missing_coverage:
                    details_missing = {"missing_requirements": missing_coverage}
                else:
                    details_missing = {}
            else:
                details_missing = {}
            if status == "not_applicable" and mandatory:
                status = "inconclusive"
            details = _dump(result)
            if not isinstance(details, Mapping):
                details = {"result": details}
            details = dict(details)
            details.update(details_missing)
            stage = StageResult(name=name.value, status=status, mandatory=mandatory,
                                started_at=started, finished_at=_now(),
                                tool=str(details.get("tool")) if details.get("tool") else None,
                                tool_version=str(details.get("tool_version")) if details.get("tool_version") else None,
                                details=details)
            await emit(f"verification: stage {name.value} {status}")
            return result, stage
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("verification stage %s failed", name.value)
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            stage = StageResult(name=name.value, status="failed", mandatory=mandatory,
                                started_at=started, finished_at=_now(), details=result)
            await emit(f"verification: stage {name.value} failed: {exc}")
            return result, stage

    @staticmethod
    def _collect_links(name: StageName, result: Any,
                       links: dict[str, list[tuple[str, str]]]) -> None:
        if name is StageName.ASSERTIONS:
            for item in _result_list(result, "assertions"):
                data = _as_mapping(item)
                rid = data.get("requirement_id")
                pid = data.get("property_id", data.get("id", "assertion"))
                if rid:
                    links.setdefault(str(rid), []).append((str(pid), str(_stage_status(item))))
        elif name is StageName.FUNCTIONAL_COVERAGE:
            for item in _result_list(result, "coverage"):
                data = _as_mapping(item)
                rid = data.get("requirement_id", data.get("id"))
                if rid:
                    links.setdefault(str(rid), []).append((str(rid), str(_stage_status(item))))

    @staticmethod
    def _requirement_matrix(spec: DesignSpec, stages: list[StageResult], raw: Mapping[str, Any],
                            links: Mapping[str, list[tuple[str, str]]]) -> list[RequirementResult]:
        stage_by_name = {item.name: item for item in stages}
        coverage_ids = {
            item.requirement_id or item.id
            for item in coverage_requirements_from_design_spec(spec)
            if item.mandatory
        }
        assertion_ids = {
            item.requirement_id for item in assertions_from_design_spec(spec)
            if item.requirement_id
        }
        coverage_items = _result_list(raw.get(StageName.FUNCTIONAL_COVERAGE.value, []), "coverage")
        observed_coverage = {
            str(_as_mapping(item).get("requirement_id", _as_mapping(item).get("id", "")))
            for item in coverage_items
        }
        assertion_items = _result_list(raw.get(StageName.ASSERTIONS.value, []), "assertions")
        observed_assertions = {
            str(_as_mapping(item).get("requirement_id", "")) for item in assertion_items
        }
        out: list[RequirementResult] = []
        for requirement in spec.requirements:
            evidence: list[str] = []
            states = [status for _, status in links.get(requirement.id, ())]
            coverage_missing = requirement.id in coverage_ids and requirement.id not in observed_coverage
            assertion_missing = requirement.id in assertion_ids and requirement.id not in observed_assertions
            if coverage_missing or assertion_missing:
                status = "inconclusive"
                evidence = [StageName.FUNCTIONAL_COVERAGE.value if coverage_missing
                            else StageName.ASSERTIONS.value]
            elif states:
                if "failed" in states:
                    status = "failed"
                elif "inconclusive" in states:
                    status = "inconclusive"
                else:
                    status = "passed"
                evidence = [item for item, _ in links[requirement.id]]
            elif requirement.id in coverage_ids or requirement.id in assertion_ids:
                status = "inconclusive"
                evidence = [StageName.FUNCTIONAL_COVERAGE.value if requirement.id in coverage_ids
                            else StageName.ASSERTIONS.value]
            elif requirement.kind in {"functional", "verification"}:
                diff = stage_by_name.get(StageName.DIFFERENTIAL.value)
                if diff is None or diff.status in {"pending", "running", "inconclusive"}:
                    status = "inconclusive"
                elif diff.status == "failed":
                    status = "failed"
                else:
                    status = "passed"
                evidence = [StageName.DIFFERENTIAL.value]
            elif requirement.kind == "hardware":
                hardware = stage_by_name.get(StageName.HARDWARE.value)
                if hardware is None or hardware.status in {"pending", "running", "inconclusive"}:
                    status = "inconclusive"
                elif hardware.status == "failed":
                    status = "failed"
                else:
                    status = "passed"
                evidence = [StageName.HARDWARE.value]
            else:
                status = "not_tested"
            out.append(RequirementResult(requirement_id=requirement.id, status=status,
                                         evidence=evidence,
                                         notes=None if status == "passed" else
                                         "mandatory verification evidence is not passing"))
        return out

    @staticmethod
    def _gate(stages: list[StageResult], matrix: list[RequirementResult]) -> VerificationGate:
        blocking_stages = tuple(item.name for item in stages
                                if item.mandatory and item.status in {"failed", "inconclusive", "pending", "running"})
        blocking_requirements = tuple(item.requirement_id for item in matrix
                                      if item.status in {"failed", "inconclusive", "not_tested"})
        if any(item.status == "failed" for item in stages if item.mandatory) or \
                any(item.status == "failed" for item in matrix):
            status = GateStatus.FAILED
        elif blocking_stages or blocking_requirements:
            status = GateStatus.INCONCLUSIVE
        else:
            status = GateStatus.PASSED
        allowed = status is GateStatus.PASSED
        reason = ("all mandatory verification gates passed" if allowed else
                  "mandatory verification gates are not signoff-clean: " +
                  ", ".join(blocking_stages + blocking_requirements))
        return VerificationGate(allowed, status, blocking_stages, blocking_requirements, reason)


async def run_verification_job(job_manager: Any, pipeline: VerificationPipeline,
                               spec: DesignSpec, rtl_code: str, *,
                               testbench_code: str = "", target: str | None = None,
                               reference_rtl_code: str | None = None,
                               session: str | None = None, persist: bool = True) -> VerificationRun:
    """Submit one verification job and await it without tying it to a WS."""
    if pipeline._default_adapters and pipeline.manager is not job_manager:
        pipeline.manager = job_manager
        pipeline.adapters = default_verification_adapters(
            manager=job_manager, repository=pipeline.repository)
    holder: dict[str, VerificationRun] = {}

    async def runner(job: Any) -> str:
        result = await pipeline.run(spec, rtl_code, testbench_code=testbench_code,
                                    target=target, reference_rtl_code=reference_rtl_code,
                                    session=session, persist=persist, job=job)
        holder["result"] = result
        return json.dumps(result.as_dict(), sort_keys=True, default=str)

    job = job_manager.submit(
        "verification", {"top_module": spec.top_module, "design_id": spec.design_id},
        runner=runner, design=spec.top_module, session=session,
    )
    await job.wait()
    result = replace(holder["result"], job_id=job.id)
    job.manifest = {"verification": result.as_dict()}
    return result


def require_synthesis_allowed(result: VerificationRun | RunManifest | Mapping[str, Any]) -> None:
    """Compatibility helper for synthesis/delivery callers.

    Call this immediately before synthesis.  It accepts the rich pipeline
    result, a manifest, or the serialized job/API form.
    """
    if isinstance(result, VerificationRun):
        result.gate.require()
        return
    if isinstance(result, RunManifest):
        metadata = result.metadata.get("verification_gate", {})
    else:
        metadata = result.get("gate", result.get("metadata", {}).get("verification_gate", {}))
    allowed = bool(metadata.get("allowed", False))
    if not allowed:
        gate = VerificationGate(
            False, GateStatus(str(metadata.get("status", "inconclusive"))),
            tuple(metadata.get("blocking_stages", ())),
            tuple(metadata.get("blocking_requirements", ())),
            str(metadata.get("reason", "verification gate did not pass")),
        )
        gate.require()


__all__ = [
    "GateStatus", "StageName", "VerificationAdapters", "VerificationGate",
    "VerificationGateError", "VerificationPipeline", "VerificationRun",
    "require_synthesis_allowed", "run_verification_job",
]
