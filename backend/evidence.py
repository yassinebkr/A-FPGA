"""Versioned design evidence and durable run storage.

The agent's chat history is useful for interaction, but it is not a suitable
source of truth for a hardware result.  This module defines the small,
versioned records that make a run auditable:

* :class:`DesignSpec` describes the requested behaviour independently of RTL.
* :class:`RunManifest` records the exact inputs, tools, stages and metrics.
* :class:`EvidenceReport` is derived from a manifest for human-facing review.

Completed manifests are immutable once persisted.  JSON is the canonical
wire/storage format; YAML support is deliberately optional and uses only
``yaml.safe_load``/``safe_dump`` when PyYAML is installed.
"""

from __future__ import annotations

import hashlib
import math
import json
import os
import re
import tempfile
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from constraints import ConstraintSet


SCHEMA_VERSION = "1.0"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(ValueError):
    """Base error for invalid evidence records or persistence operations."""


class ImmutableRunError(EvidenceError):
    """Raised when a persisted completed run is changed."""


class EvidenceYamlUnavailable(EvidenceError):
    """Raised when optional YAML support is requested without PyYAML."""


class EvidenceAssurance(str, Enum):
    """What a result is evidence of, not how good the RTL is.

    A passing simulation is not an FPGA implementation result, and an
    open-source demo-PDK flow is not foundry signoff.
    """

    SIMULATION = "simulation"
    FPGA_IMPLEMENTATION = "fpga_implementation"
    DEMO_PDK_ESTIMATE = "demo_pdk_estimate"
    FOUNDRY_SIGNOFF = "foundry_signoff"
    UNCLASSIFIED = "unclassified"


AssuranceClass = EvidenceAssurance


class _Model(BaseModel):
    """Pydantic v1/v2-compatible strict base for evidence records."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        # model_dump is Pydantic v2; json()/parse is compatible with v1.
        try:
            return value.model_dump(mode="json")
        except AttributeError:
            return json.loads(value.json())
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    return value


def canonical_data(value: Any) -> dict | list | str | int | float | bool | None:
    """Return JSON-compatible data in a deterministic representation."""
    return _plain(value)


def canonical_json(value: Any) -> str:
    """Serialize a record deterministically for hashing and persistence."""
    return json.dumps(canonical_data(value), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_record(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


class InterfaceSpec(_Model):
    name: str
    direction: Literal["input", "output", "inout"]
    width: int = Field(default=1, ge=1)
    protocol: str | None = None
    description: str | None = None

    @field_validator("name")
    def valid_name(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("interface name must be a portable HDL identifier")
        return value


class ClockDomain(_Model):
    name: str
    clock_port: str
    reset_port: str | None = None
    frequency_mhz: float | None = Field(default=None, gt=0)
    reset_polarity: Literal["active_high", "active_low"] = "active_high"

    @field_validator("frequency_mhz")
    def finite_frequency(cls, value):
        if value is not None and not math.isfinite(value):
            raise ValueError("frequency_mhz must be finite")
        return value


class Requirement(_Model):
    id: str
    text: str
    kind: Literal["functional", "timing", "resource", "power",
                   "verification", "hardware", "other"] = "functional"
    mandatory: bool = True

    @field_validator("id")
    def valid_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("requirement id contains unsafe characters")
        return value


class AssertionSpec(_Model):
    """A DesignSpec-owned SVA/property declaration.

    The expression is intentionally text: the eventual HDL frontend owns the
    grammar, while this schema validates the audit metadata and keeps the
    property attached to a requirement.
    """

    id: str | None = None
    name: str | None = None
    expression: str
    kind: Literal["assert", "assume", "cover"] = "assert"
    clock: str | None = None
    disable_iff: str | None = None
    requirement_id: str | None = None
    source: str = "design_spec"

    @field_validator("expression")
    def nonempty_expression(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("assertion expression must not be empty")
        return value.strip()


class CoverageRequirementSpec(_Model):
    """One functional coverage obligation in the DesignSpec."""

    id: str
    target: str
    kind: Literal["functional", "code", "cross", "assertion"] = "functional"
    threshold: float | None = Field(default=None, ge=0, le=1)
    mandatory: bool = True
    requirement_id: str | None = None
    # Explicit event contract consumed by functional coverage. ``field`` is
    # looked up in each differential sample; ``bins`` are legal JSON scalar
    # values. ``cross`` names other coverage ids to cross.
    field: str | None = None
    bins: list[Any] = Field(default_factory=list)
    cross: list[str] = Field(default_factory=list)

    @field_validator("id", "target")
    def nonempty_coverage_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("coverage id/target must not be empty")
        return value.strip()


class WaiverScopeSpec(_Model):
    """Validated scope for a lint waiver; at least one boundary is required."""

    rule_id: str | None = None
    top_module: str | None = None
    design_class: str | None = None
    file_glob: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def bounded(self):
        if self.line_start is not None and self.line_end is not None and self.line_end < self.line_start:
            raise ValueError("waiver line_end must be >= line_start")
        if not any((self.rule_id, self.top_module, self.design_class, self.file_glob,
                    self.line_start is not None, self.line_end is not None)):
            raise ValueError("waiver scope must constrain at least one field")
        return self


class WaiverSpec(_Model):
    """An expiring, attributable exception to the lint policy."""

    id: str
    reason: str
    author: str
    scope: WaiverScopeSpec
    expires: date
    ticket: str | None = None

    @field_validator("id", "reason", "author")
    def nonempty_waiver_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("waiver metadata must not be empty")
        return value.strip()


class VerificationPlan(_Model):
    directed_vectors: int = Field(default=0, ge=0)
    randomized_vectors: int = Field(default=0, ge=0)
    requires_formal: bool = False
    requires_equivalence: bool = False
    requires_cdc_rdc: bool = False
    requires_hardware: bool = False
    formal_mode: Literal["prove", "bmc", "cover"] = "prove"
    formal_depth: int = Field(default=20, ge=1, le=100000)
    coverage_target_percent: float | None = Field(default=None, ge=0, le=100)
    design_class: str = "unknown"
    assertions: list[AssertionSpec] = Field(default_factory=list)
    # ``properties`` is accepted as a first-class spelling because the
    # frontend/spec importer uses that term for generic temporal properties.
    properties: list[AssertionSpec] = Field(default_factory=list)
    coverage_requirements: list[CoverageRequirementSpec] = Field(default_factory=list)
    lint_rules: dict[str, Literal["error", "warning", "info"]] = Field(default_factory=dict)
    waivers: list[WaiverSpec] = Field(default_factory=list)
    # Validated by cdc_rdc at execution time; kept here to avoid a module cycle.
    cdc_waivers: list[dict[str, Any]] = Field(default_factory=list)


class DesignSpec(_Model):
    schema_version: str = SCHEMA_VERSION
    design_id: str
    revision: int = Field(default=1, ge=1)
    name: str
    top_module: str
    target: str = "unspecified"
    interfaces: list[InterfaceSpec] = Field(default_factory=list)
    clock_domains: list[ClockDomain] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    timing_goals: dict[str, Any] = Field(default_factory=dict)
    # Explicit expert constraints are typed and hashed as part of the spec.
    # timing_goals remains for compatibility with older project records.
    constraints: ConstraintSet = Field(default_factory=ConstraintSet)
    verification: VerificationPlan = Field(default_factory=VerificationPlan)
    assumptions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("design_id", "name")
    def nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value

    @field_validator("top_module")
    def valid_top(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("top_module must be a portable HDL identifier")
        return value

    @field_validator("schema_version")
    def supported_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported evidence schema version: {value}")
        return value


class ArtifactRef(_Model):
    sha256: str
    name: str
    media_type: str = "application/octet-stream"
    size_bytes: int = Field(ge=0)
    locator: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sha256")
    def valid_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be a lowercase 64-character digest")
        return value


class StageResult(_Model):
    name: str
    status: Literal["pending", "running", "passed", "failed",
                   "inconclusive", "skipped", "not_applicable"]
    mandatory: bool = True
    tool: str | None = None
    tool_version: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    artifact_hashes: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    # Derived in the report; an input value cannot elevate the report by itself.
    assurance: EvidenceAssurance = EvidenceAssurance.UNCLASSIFIED

    @field_validator("artifact_hashes")
    def valid_artifact_hash(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _SHA256.fullmatch(value):
                raise ValueError("stage artifact references must be SHA-256 digests")
        return values


class MetricValue(_Model):
    name: str
    value: float | int | str | None
    unit: str
    stage: str
    target: str
    corner: str
    tool: str
    tool_version: str
    artifact_hashes: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    # Derived provenance class for this individual measurement.
    assurance: EvidenceAssurance = EvidenceAssurance.UNCLASSIFIED
    @field_validator("artifact_hashes")
    def valid_metric_hash(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _SHA256.fullmatch(value):
                raise ValueError("metric artifact references must be SHA-256 digests")
        return values


class RequirementResult(_Model):
    requirement_id: str
    status: Literal["passed", "failed", "inconclusive", "not_tested",
                   "not_applicable"]
    evidence: list[str] = Field(default_factory=list)
    artifact_hashes: list[str] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("artifact_hashes")
    def valid_requirement_hash(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _SHA256.fullmatch(value):
                raise ValueError("requirement artifact references must be SHA-256 digests")
        return values


class RunManifest(_Model):
    schema_version: str = SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: f"run-{uuid.uuid4().hex}")
    design_id: str
    design_revision: int = Field(ge=1)
    spec_sha256: str
    parent_run_id: str | None = None
    status: Literal["planned", "running", "completed", "failed",
                   "cancelled", "inconclusive"] = "planned"
    created_at: datetime = Field(default_factory=_now)
    completed_at: datetime | None = None
    target: str
    rtl_sha256: str | None = None
    testbench_sha256: str | None = None
    constraints_sha256: str | None = None
    # Exact constraints consumed by an implementation adapter. The semantic
    # constraints_sha256 identifies the typed ConstraintSet; this reference
    # identifies the byte-for-byte rendered SDC/XDC input.
    constraints_artifact: ArtifactRef | None = None
    # Optional producer declaration.  The derived report still verifies
    # provenance and never treats this field as proof of signoff.
    assurance: EvidenceAssurance | None = None
    toolchain: dict[str, str] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    stages: list[StageResult] = Field(default_factory=list)
    metrics: list[MetricValue] = Field(default_factory=list)
    requirement_results: list[RequirementResult] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    def supported_manifest_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported evidence schema version: {value}")
        return value

    @field_validator("spec_sha256", "rtl_sha256", "testbench_sha256",
                       "constraints_sha256")
    def valid_optional_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("record hashes must be lowercase SHA-256 digests")
        return value

    @model_validator(mode="after")
    def validate_manifest(self):
        if self.status == "completed" and self.completed_at is None:
            raise ValueError("completed runs require completed_at")
        declared = {artifact.sha256 for artifact in self.artifacts}
        referenced = {digest for stage in self.stages for digest in stage.artifact_hashes}
        referenced.update(digest for metric in self.metrics for digest in metric.artifact_hashes)
        referenced.update(digest for result in self.requirement_results for digest in result.artifact_hashes)
        missing = sorted(referenced - declared)
        if missing:
            raise ValueError("manifest references artifacts not present in artifacts: " + ", ".join(missing))
        if self.constraints_artifact is not None:
            if self.constraints_sha256 is None:
                raise ValueError("constraints_artifact requires constraints_sha256")
            if self.constraints_artifact.sha256 not in declared:
                raise ValueError("constraints_artifact must be present in artifacts")
            metadata = self.constraints_artifact.metadata
            if metadata.get("kind") != "constraints":
                raise ValueError("constraints_artifact metadata.kind must be constraints")
            if metadata.get("constraints_sha256") != self.constraints_sha256:
                raise ValueError("constraints_artifact semantic hash does not match constraints_sha256")
            if metadata.get("format") not in {"sdc", "xdc"}:
                raise ValueError("constraints_artifact metadata.format must be sdc or xdc")
        return self


class RequirementEvidence(_Model):
    requirement_id: str
    status: Literal["passed", "failed", "inconclusive", "not_tested",
                   "not_applicable"]
    evidence: list[str] = Field(default_factory=list)
    notes: str | None = None


AnalysisStatus = Literal["available", "inconclusive", "unavailable"]


class ToolVersion(_Model):
    """A tool identity recorded with expert implementation evidence."""

    tool: str
    version: str | None = None
    executable: str | None = None
    source: str | None = None
    artifact_hashes: list[str] = Field(default_factory=list)

    @field_validator("tool")
    def nonempty_tool(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tool name must not be empty")
        return value.strip()

    @field_validator("artifact_hashes")
    def valid_tool_artifact_hash(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _SHA256.fullmatch(value):
                raise ValueError("tool artifact references must be SHA-256 digests")
        return values


class TimingPath(_Model):
    """One reported setup/hold path, when the timing tool provides paths."""

    startpoint: str
    endpoint: str
    analysis_type: Literal["setup", "hold", "unknown"] = "unknown"
    slack_ns: float | None = None
    delay_ns: float | None = None
    path_group: str | None = None
    clock: str | None = None
    logic_depth: int | None = Field(default=None, ge=0)
    artifact_hashes: list[str] = Field(default_factory=list)

    @field_validator("startpoint", "endpoint")
    def nonempty_path_endpoint(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("timing path endpoints must not be empty")
        return value.strip()

    @field_validator("slack_ns", "delay_ns")
    def finite_timing_value(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("timing values must be finite")
        return value

    @field_validator("artifact_hashes")
    def valid_path_artifact_hash(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _SHA256.fullmatch(value):
                raise ValueError("timing path artifact references must be SHA-256 digests")
        return values


class TimingReport(_Model):
    status: AnalysisStatus = "unavailable"
    paths: list[TimingPath] = Field(default_factory=list)
    worst_setup_slack_ns: float | None = None
    worst_hold_slack_ns: float | None = None
    fmax_mhz: float | None = None
    clock_period_ns: float | None = None
    clock_port: str | None = None
    tool: str | None = None
    tool_version: str | None = None
    artifact_hashes: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("artifact_hashes")
    def valid_report_artifact_hash(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _SHA256.fullmatch(value):
                raise ValueError("timing report artifact references must be SHA-256 digests")
        return values


class AreaReport(_Model):
    status: AnalysisStatus = "unavailable"
    cell_area_um2: float | None = None
    total_area_um2: float | None = None
    utilization_percent: float | None = None
    cell_count: int | None = Field(default=None, ge=0)
    register_count: int | None = Field(default=None, ge=0)
    net_count: int | None = Field(default=None, ge=0)
    tool: str | None = None
    tool_version: str | None = None
    artifact_hashes: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("artifact_hashes")
    def valid_report_artifact_hash(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _SHA256.fullmatch(value):
                raise ValueError("area report artifact references must be SHA-256 digests")
        return values


class PowerReport(_Model):
    """Power values plus the assumptions that make estimates meaningful."""

    status: AnalysisStatus = "unavailable"
    total_mw: float | None = None
    dynamic_mw: float | None = None
    leakage_mw: float | None = None
    voltage_v: float | None = None
    frequency_mhz: float | None = None
    activity_factor: float | None = Field(default=None, ge=0, le=1)
    tool: str | None = None
    tool_version: str | None = None
    artifact_hashes: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("artifact_hashes")
    def valid_report_artifact_hash(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _SHA256.fullmatch(value):
                raise ValueError("power report artifact references must be SHA-256 digests")
        return values


class ImplementationReport(_Model):
    """Expert-facing implementation evidence, separate from generic metrics."""

    status: AnalysisStatus = "unavailable"
    timing: TimingReport = Field(default_factory=TimingReport)
    area: AreaReport = Field(default_factory=AreaReport)
    power: PowerReport = Field(default_factory=PowerReport)
    tool_versions: list[ToolVersion] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class EvidenceReport(_Model):
    schema_version: str = SCHEMA_VERSION
    report_id: str = Field(default_factory=lambda: f"report-{uuid.uuid4().hex}")
    run_id: str
    design_id: str
    spec_sha256: str
    constraints_sha256: str | None = None
    constraints_artifact: ArtifactRef | None = None
    derived_at: datetime = Field(default_factory=_now)
    overall_status: Literal["passed", "failed", "inconclusive", "not_tested"]
    confidence: Literal["exploratory", "fpga_validated", "manufacturing_candidate"]
    # Precise evidence provenance; confidence remains backwards compatible.
    assurance: EvidenceAssurance = EvidenceAssurance.UNCLASSIFIED
    evidence_classes: list[EvidenceAssurance] = Field(default_factory=list)
    assurance_basis: list[str] = Field(default_factory=list)
    stages: list[StageResult] = Field(default_factory=list)
    metrics: list[MetricValue] = Field(default_factory=list)
    requirements: list[RequirementEvidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    artifact_hashes: list[str] = Field(default_factory=list)
    implementation: ImplementationReport = Field(default_factory=ImplementationReport)


def _metric_record(metrics: list[MetricValue], *names: str) -> MetricValue | None:
    wanted = {name.lower() for name in names}
    return next((item for item in metrics if item.name.lower() in wanted and item.value is not None), None)


def _metric_number(metric: MetricValue | None) -> float | None:
    if metric is None:
        return None
    try:
        if isinstance(metric.value, str):
            match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", metric.value)
            return float(match.group(0)) if match else None
        value = float(metric.value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _metric_tool(metrics: list[MetricValue], *names: str) -> tuple[str | None, str | None]:
    metric = _metric_record(metrics, *names)
    return (metric.tool, metric.tool_version) if metric else (None, None)


def _metadata_section(manifest: RunManifest, name: str) -> Mapping[str, Any]:
    value = manifest.metadata.get(name)
    return value if isinstance(value, Mapping) else {}


def _implementation_report(manifest: RunManifest) -> ImplementationReport:
    """Build the typed expert view without manufacturing missing measurements."""
    metrics = manifest.metrics
    implementation_meta = _metadata_section(manifest, "implementation")
    timing_meta = implementation_meta.get("timing") if isinstance(implementation_meta.get("timing"), Mapping) else _metadata_section(manifest, "timing")
    area_meta = implementation_meta.get("area") if isinstance(implementation_meta.get("area"), Mapping) else _metadata_section(manifest, "area")
    power_meta = implementation_meta.get("power") if isinstance(implementation_meta.get("power"), Mapping) else _metadata_section(manifest, "power")

    setup = _metric_record(metrics, "setupslack", "worst_setup_slack")
    hold = _metric_record(metrics, "holdslack", "worst_hold_slack")
    fmax = _metric_record(metrics, "fmax", "fmax_mhz")
    period = manifest.constraints.get("clock_period_ns")
    try:
        period = float(period) if period is not None else None
        if period is not None and not math.isfinite(period):
            period = None
    except (TypeError, ValueError):
        period = None
    fmax_value = _metric_number(fmax)
    if fmax is not None and fmax.unit.lower() in {"hz", "1/s"} and fmax_value is not None:
        fmax_value /= 1_000_000.0
    timing_paths: list[TimingPath] = []
    raw_paths = timing_meta.get("paths", timing_meta.get("timing_paths", [])) if timing_meta else []
    if isinstance(raw_paths, list):
        for raw in raw_paths:
            if not isinstance(raw, Mapping):
                continue
            candidate = {key: raw[key] for key in (
                "startpoint", "endpoint", "analysis_type", "slack_ns",
                "delay_ns", "path_group", "clock", "logic_depth",
                "artifact_hashes") if key in raw}
            if "startpoint" not in candidate or "endpoint" not in candidate:
                continue
            try:
                timing_paths.append(TimingPath(**candidate))
            except (TypeError, ValueError):
                continue
    timing_tool, timing_version = _metric_tool(metrics, "setupslack", "holdslack", "fmax")
    timing_values = any(_metric_number(item) is not None for item in (setup, hold, fmax))
    timing_stage = next((stage for stage in manifest.stages if stage.name in {"timing", "timing_signoff", "synthesis"}), None)
    timing_status: AnalysisStatus = ("available" if timing_values or timing_paths else
                                     "inconclusive" if timing_stage else "unavailable")
    timing = TimingReport(
        status=timing_status, paths=timing_paths,
        worst_setup_slack_ns=_metric_number(setup),
        worst_hold_slack_ns=_metric_number(hold), fmax_mhz=fmax_value,
        clock_period_ns=period, clock_port=manifest.constraints.get("clock_port"),
        tool=timing_tool, tool_version=timing_version,
        artifact_hashes=sorted({digest for item in (setup, hold, fmax) if item for digest in item.artifact_hashes}),
        assumptions=list(timing_meta.get("assumptions", [])) if isinstance(timing_meta.get("assumptions", []), list) else [],
    )

    cell_area = _metric_record(metrics, "cellarea", "cell_area_um2")
    total_area = _metric_record(metrics, "totalarea", "total_area_um2")
    utilization = _metric_record(metrics, "utilization", "utilization_percent")
    cells = _metric_record(metrics, "cells", "cell_count")
    registers = _metric_record(metrics, "registers", "register_count")
    nets = _metric_record(metrics, "nets", "net_count")
    area_values = [_metric_number(item) for item in (cell_area, total_area, utilization, cells, registers, nets)]
    area_tool, area_version = _metric_tool(metrics, "cellarea", "totalarea", "utilization", "cells")
    area_stage = next((stage for stage in manifest.stages if stage.name in {"synthesis", "place", "route"}), None)
    area_status: AnalysisStatus = ("available" if any(value is not None for value in area_values) else
                                   "inconclusive" if area_stage else "unavailable")
    area = AreaReport(
        status=area_status, cell_area_um2=_metric_number(cell_area),
        total_area_um2=_metric_number(total_area), utilization_percent=_metric_number(utilization),
        cell_count=int(_metric_number(cells)) if _metric_number(cells) is not None and _metric_number(cells) >= 0 else None,
        register_count=int(_metric_number(registers)) if _metric_number(registers) is not None and _metric_number(registers) >= 0 else None,
        net_count=int(_metric_number(nets)) if _metric_number(nets) is not None and _metric_number(nets) >= 0 else None,
        tool=area_tool, tool_version=area_version,
        artifact_hashes=sorted({digest for item in (cell_area, total_area, utilization, cells, registers, nets) if item for digest in item.artifact_hashes}),
        assumptions=list(area_meta.get("assumptions", [])) if isinstance(area_meta.get("assumptions", []), list) else [],
    )

    total_power = _metric_record(metrics, "power", "totalpower", "peakpower", "total_power_mw")
    dynamic_power = _metric_record(metrics, "dynamicpower", "dynamic_power_mw")
    leakage_power = _metric_record(metrics, "leakagepower", "leakage_power_mw")
    power_values = [_metric_number(item) for item in (total_power, dynamic_power, leakage_power)]
    power_tool, power_version = _metric_tool(metrics, "power", "totalpower", "peakpower", "leakagepower")
    raw_assumptions = power_meta.get("assumptions", []) if power_meta else manifest.metadata.get("power_assumptions", [])
    power_assumptions = [str(item) for item in raw_assumptions] if isinstance(raw_assumptions, list) else []
    power_stage = next((stage for stage in manifest.stages if stage.name in {"power", "synthesis", "place", "route"}), None)
    power_status: AnalysisStatus = ("available" if any(value is not None for value in power_values) else
                                    "inconclusive" if power_assumptions or power_stage else "unavailable")
    def _meta_float(section: Mapping[str, Any], key: str) -> float | None:
        try:
            value = float(section[key]) if key in section else None
            if value is None or not math.isfinite(value):
                return None
            if key == "activity_factor" and not 0 <= value <= 1:
                return None
            return value
        except (TypeError, ValueError):
            return None
    power = PowerReport(
        status=power_status, total_mw=_metric_number(total_power),
        dynamic_mw=_metric_number(dynamic_power), leakage_mw=_metric_number(leakage_power),
        voltage_v=_meta_float(power_meta, "voltage_v"), frequency_mhz=_meta_float(power_meta, "frequency_mhz"),
        activity_factor=_meta_float(power_meta, "activity_factor"), tool=power_tool,
        tool_version=power_version,
        artifact_hashes=sorted({digest for item in (total_power, dynamic_power, leakage_power) if item for digest in item.artifact_hashes}),
        assumptions=power_assumptions,
    )

    tool_versions: list[ToolVersion] = []
    for tool, version in manifest.toolchain.items():
        try:
            tool_versions.append(ToolVersion(tool=str(tool), version=str(version)))
        except ValueError:
            continue
    raw_tools = implementation_meta.get("tool_versions", [])
    if isinstance(raw_tools, list):
        for raw in raw_tools:
            if isinstance(raw, Mapping):
                try:
                    tool_versions.append(ToolVersion(**{key: raw[key] for key in ("tool", "version", "executable", "source", "artifact_hashes") if key in raw}))
                except (TypeError, ValueError):
                    continue
    all_statuses = (timing.status, area.status, power.status)
    report_status: AnalysisStatus = ("available" if all(status == "available" for status in all_statuses) else
                                     "inconclusive" if any(status != "unavailable" for status in all_statuses) else "unavailable")
    limitations: list[str] = []
    if timing.status == "unavailable":
        limitations.append("timing analysis is unavailable")
    elif not timing.paths:
        limitations.append("timing summary is available, but detailed timing paths were not recorded")
    if area.status == "unavailable":
        limitations.append("utilization/area analysis is unavailable")
    if power.status != "available":
        limitations.append("power is not measured; any power result requires explicit voltage, frequency, and activity assumptions")
    assumptions = list(manifest.metadata.get("assumptions", [])) if isinstance(manifest.metadata.get("assumptions", []), list) else []
    assumptions.extend(str(item) for item in manifest.warnings if item not in assumptions)
    return ImplementationReport(status=report_status, timing=timing, area=area, power=power,
                                tool_versions=tool_versions, artifact_refs=list(manifest.artifacts),
                                assumptions=assumptions, limitations=limitations)



_DEMO_PDK_TARGETS = {
    "skywater130_demo", "sky130_demo", "freepdk45_demo", "asap7_demo",
    "gf180_demo", "gf180mcu_demo",
}
_SIMULATION_STAGES = {"simulation", "sim", "functional_simulation", "differential"}
_IMPLEMENTATION_STAGES = {
    "synthesis", "timing", "timing_signoff", "place", "route", "implementation",
    "fpga", "fpga_implementation", "hardware",
}
_SIMULATION_TOOLS = {"iverilog", "verilator", "cocotb", "questa", "modelsim", "xsim"}
_FPGA_HINTS = (
    "fpga", "vivado", "quartus", "nextpnr", "ice40", "ecp5", "xilinx",
    "altera", "intel", "lattice", "gowin",
)
_DEMO_HINTS = (
    "demo", "pdk", "sky130", "skywater", "freepdk", "asap7", "gf180",
    "openroad", "opensta", "siliconcompiler",
)


def _assurance_text(value: Any) -> str:
    if isinstance(value, EvidenceAssurance):
        return value.value
    return str(value or "").strip().lower().replace("-", "_")


def _manifest_declared_assurance(manifest: RunManifest) -> str:
    """Read a declaration only as a signoff *request*, never as proof."""
    if manifest.assurance is not None:
        return _assurance_text(manifest.assurance)
    for key in ("assurance", "evidence_class", "evidence_assurance"):
        value = manifest.metadata.get(key)
        if isinstance(value, str):
            return _assurance_text(value)
    return ""


def _is_explicit_foundry_signoff(manifest: RunManifest, overall: str) -> bool:
    """Require auditable approvals before exposing a foundry claim.

    Stage names alone are deliberately insufficient: ``drc``/``lvs`` from an
    open demo flow must not become foundry signoff.  Producers must identify
    the approved PDK/rule deck and independent review, and record the core
    signoff stages as passed.
    """
    if overall != "passed" or _manifest_declared_assurance(manifest) != EvidenceAssurance.FOUNDRY_SIGNOFF.value:
        return False
    metadata = manifest.metadata
    signoff = metadata.get("signoff")
    if not isinstance(signoff, Mapping):
        signoff = {}
    approved_pdk = signoff.get("approved_pdk", metadata.get("approved_pdk"))
    rule_deck = signoff.get("rule_deck", metadata.get("rule_deck"))
    reviewed = signoff.get("independent_review", metadata.get("independent_review"))
    if not isinstance(approved_pdk, str) or not approved_pdk.strip():
        return False
    if not isinstance(rule_deck, str) or not rule_deck.strip():
        return False
    if reviewed is not True:
        return False
    target = _assurance_text(manifest.target)
    # A real foundry PDK name often contains ``pdk`` or a process name such
    # as ``sky130``.  Reject only an explicit demo target here; the approvals
    # and required stages above are what distinguish signoff from an estimate.
    if target in _DEMO_PDK_TARGETS or "demo" in target:
        return False
    stages = {stage.name.lower().strip(): stage for stage in manifest.stages}
    required = signoff.get("required_stages", metadata.get("required_signoff_stages"))
    if not isinstance(required, list) or not required:
        required = ["drc", "lvs", "timing_signoff"]
    return all(
        isinstance(name, str) and stages.get(name.lower().strip()) is not None
        and stages[name.lower().strip()].status == "passed"
        for name in required
    )


def _classify_evidence_item(*, stage: str = "", target: str = "", tool: str = "",
                            toolchain: Mapping[str, str] | None = None) -> EvidenceAssurance:
    stage_text = _assurance_text(stage)
    target_text = _assurance_text(target)
    tool_text = _assurance_text(tool)
    tools = " ".join(_assurance_text(item) for item in (toolchain or {}).keys())
    if stage_text in _SIMULATION_STAGES or tool_text in _SIMULATION_TOOLS:
        return EvidenceAssurance.SIMULATION
    if (any(hint in target_text for hint in _FPGA_HINTS)
            or any(hint in tool_text for hint in _FPGA_HINTS)
            or (stage_text in {"fpga", "fpga_implementation", "hardware"}
                and not any(hint in target_text for hint in _DEMO_HINTS))):
        return EvidenceAssurance.FPGA_IMPLEMENTATION
    if (target_text in _DEMO_PDK_TARGETS
            or any(hint in target_text for hint in _DEMO_HINTS)
            or any(hint in tool_text for hint in _DEMO_HINTS)
            or any(hint in tools for hint in _DEMO_HINTS)):
        return EvidenceAssurance.DEMO_PDK_ESTIMATE
    return EvidenceAssurance.UNCLASSIFIED


def _assurance_rank(value: EvidenceAssurance) -> int:
    # A report summarizes the strongest *supported* class; simulation is not
    # allowed to hide implementation evidence, and no class outranks signoff.
    return {
        EvidenceAssurance.UNCLASSIFIED: 0,
        EvidenceAssurance.SIMULATION: 1,
        EvidenceAssurance.DEMO_PDK_ESTIMATE: 2,
        EvidenceAssurance.FPGA_IMPLEMENTATION: 3,
        EvidenceAssurance.FOUNDRY_SIGNOFF: 4,
    }[value]


def _derive_assurance(manifest: RunManifest, overall: str) -> tuple[EvidenceAssurance, list[EvidenceAssurance], list[str]]:
    classes: list[EvidenceAssurance] = []
    for stage in manifest.stages:
        value = _classify_evidence_item(stage=stage.name, target=manifest.target,
                                        tool=stage.tool or "", toolchain=manifest.toolchain)
        if value not in classes:
            classes.append(value)
    for metric in manifest.metrics:
        value = _classify_evidence_item(stage=metric.stage, target=metric.target,
                                        tool=metric.tool, toolchain=manifest.toolchain)
        if value not in classes:
            classes.append(value)
    if _is_explicit_foundry_signoff(manifest, overall):
        classes.append(EvidenceAssurance.FOUNDRY_SIGNOFF)
    classes = sorted(classes, key=_assurance_rank)
    if not classes:
        classes = [EvidenceAssurance.UNCLASSIFIED]
    # A mixed run may contain simulation, FPGA, and ASIC-estimate records.
    # Prefer the conservative ASIC estimate when it is present rather than
    # letting one FPGA stage make the whole package sound FPGA-only.
    if EvidenceAssurance.FOUNDRY_SIGNOFF in classes:
        assurance = EvidenceAssurance.FOUNDRY_SIGNOFF
    elif EvidenceAssurance.DEMO_PDK_ESTIMATE in classes:
        assurance = EvidenceAssurance.DEMO_PDK_ESTIMATE
    elif EvidenceAssurance.FPGA_IMPLEMENTATION in classes:
        assurance = EvidenceAssurance.FPGA_IMPLEMENTATION
    else:
        assurance = max(classes, key=_assurance_rank)
    basis = {
        EvidenceAssurance.SIMULATION: "functional simulator output is evidence of RTL behavior only",
        EvidenceAssurance.FPGA_IMPLEMENTATION: "FPGA device/tool implementation evidence is tied to the named target",
        EvidenceAssurance.DEMO_PDK_ESTIMATE: "ASIC metrics come from a demo/open-PDK flow and are estimates, not signoff",
        EvidenceAssurance.FOUNDRY_SIGNOFF: "approved PDK/rule deck, required signoff stages, and independent review are recorded",
        EvidenceAssurance.UNCLASSIFIED: "target, stage, and tool provenance are insufficient for a stronger assurance class",
    }
    return assurance, classes, [basis[item] for item in classes]


def build_evidence_report(manifest: RunManifest,
                          spec: DesignSpec | None = None) -> EvidenceReport:
    """Derive a conservative report; missing mandatory evidence is not pass."""
    results = {item.requirement_id: item for item in manifest.requirement_results}
    required = {item.id for item in (spec.requirements if spec else []) if item.mandatory}
    requirements = [RequirementEvidence(requirement_id=item.requirement_id,
                                        status=item.status,
                                        evidence=item.evidence,
                                        notes=item.notes)
                    for item in manifest.requirement_results]
    missing = sorted(required - set(results))
    failed = any(item.status == "failed" for item in manifest.requirement_results)
    uncertain = any(item.status in {"inconclusive", "not_tested"}
                    for item in manifest.requirement_results)
    if missing:
        uncertain = True
        requirements.extend(RequirementEvidence(requirement_id=item,
                                                status="not_tested")
                            for item in missing)
    mandatory_failed_stages = any(stage.mandatory and stage.status == "failed"
                                  for stage in manifest.stages)
    mandatory_uncertain_stages = any(
        stage.mandatory and stage.status in {"pending", "running", "inconclusive"}
        for stage in manifest.stages)
    if failed or mandatory_failed_stages or manifest.status == "failed":
        overall = "failed"
    elif uncertain or mandatory_uncertain_stages or manifest.status in {
            "planned", "running", "inconclusive"}:
        overall = "inconclusive" if manifest.status != "planned" else "not_tested"
    elif manifest.status == "completed":
        overall = "passed"
    else:
        overall = "not_tested"
    stage_names = {stage.name.lower(): stage for stage in manifest.stages}
    hardware_stage = stage_names.get("hardware")
    hardware_ok = (hardware_stage is not None and hardware_stage.status == "passed"
                   and _classify_evidence_item(stage=hardware_stage.name,
                                                target=manifest.target,
                                                tool=hardware_stage.tool or "",
                                                toolchain=manifest.toolchain)
                   == EvidenceAssurance.FPGA_IMPLEMENTATION)
    # Keep the legacy confidence label aligned with the stricter assurance
    # gate. Passed stage names alone (especially from a demo-PDK flow) are
    # not sufficient evidence for a manufacturing claim.
    signoff_ok = _is_explicit_foundry_signoff(manifest, overall)
    confidence = ("manufacturing_candidate" if signoff_ok
                  else "fpga_validated" if hardware_ok and overall == "passed"
                  else "exploratory")
    assurance, evidence_classes, assurance_basis = _derive_assurance(manifest, overall)
    report_stages = [stage.model_copy(update={
        "assurance": _classify_evidence_item(stage=stage.name,
                                              target=manifest.target,
                                              tool=stage.tool or "",
                                              toolchain=manifest.toolchain)
    }) for stage in manifest.stages]
    report_metrics = []
    for metric in manifest.metrics:
        item_assurance = _classify_evidence_item(stage=metric.stage,
                                                  target=metric.target,
                                                  tool=metric.tool,
                                                  toolchain=manifest.toolchain)
        # Signoff is a property of the complete reviewed run.  Keep simulation
        # metrics labeled as simulation, but implementation metrics inherit the
        # run-level foundry class only after the strict gate above passes.
        if (assurance == EvidenceAssurance.FOUNDRY_SIGNOFF
                and item_assurance != EvidenceAssurance.SIMULATION):
            item_assurance = EvidenceAssurance.FOUNDRY_SIGNOFF
        report_metrics.append(metric.model_copy(update={"assurance": item_assurance}))
    limitations = list(manifest.warnings)
    if assurance == EvidenceAssurance.SIMULATION:
        limitations.append("simulation evidence does not establish FPGA timing, ASIC PPA, or silicon signoff")
    elif assurance == EvidenceAssurance.DEMO_PDK_ESTIMATE:
        limitations.append("demo-PDK implementation metrics are estimates, not FPGA measurements or foundry signoff")
    elif assurance == EvidenceAssurance.FPGA_IMPLEMENTATION:
        limitations.append("FPGA implementation evidence does not establish ASIC PPA or foundry signoff")
    if missing:
        limitations.append("mandatory requirements have no recorded evidence")
    if any(metric.corner in {"", "unspecified"} for metric in manifest.metrics):
        limitations.append("one or more metrics lack a concrete analysis corner")
    return EvidenceReport(run_id=manifest.run_id, design_id=manifest.design_id,
                          spec_sha256=manifest.spec_sha256,
                          constraints_sha256=manifest.constraints_sha256,
                          constraints_artifact=manifest.constraints_artifact,
                          overall_status=overall, confidence=confidence,
                          assurance=assurance, evidence_classes=evidence_classes,
                          assurance_basis=assurance_basis,
                          stages=report_stages, metrics=report_metrics,
                          requirements=requirements, limitations=limitations,
                          artifact_hashes=[artifact.sha256 for artifact in manifest.artifacts],
                          implementation=_implementation_report(manifest))


T = TypeVar("T", bound=BaseModel)


def _parse_model(model_type: type[T], data: Mapping[str, Any]) -> T:
    parse = getattr(model_type, "model_validate", None)
    if parse is not None:
        return parse(data)
    return model_type.parse_obj(data)


class EvidenceRepository:
    """Atomic storage for specs, manifests, derived reports and artifacts."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).absolute()
        self.specs = self.root / "specs"
        self.runs = self.root / "runs"
        self.reports = self.root / "reports"
        self.artifacts = self.root / "artifacts" / "sha256"

    def _safe_component(self, value: str, label: str) -> str:
        if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
            raise EvidenceError(f"invalid {label}")
        return value

    def _atomic_bytes(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                             suffix=".tmp", delete=False) as handle:
                temporary_name = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            temporary_name = None
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    def _save_json(self, path: Path, value: BaseModel, *, immutable=False) -> str:
        content = canonical_json(value).encode("utf-8")
        if immutable and path.exists():
            old = path.read_bytes()
            if old != content:
                raise ImmutableRunError(f"completed run is immutable: {path.stem}")
            return sha256_bytes(content)
        self._atomic_bytes(path, content)
        return sha256_bytes(content)

    def save_spec(self, spec: DesignSpec) -> str:
        path = self.specs / self._safe_component(spec.design_id, "design_id") \
            / f"r{spec.revision}.json"
        if path.exists() and path.read_bytes() != canonical_json(spec).encode("utf-8"):
            raise ImmutableRunError(f"spec revision is immutable: {spec.design_id}/r{spec.revision}")
        return self._save_json(path, spec, immutable=True)

    def load_spec(self, design_id: str, revision: int) -> DesignSpec | None:
        path = self.specs / self._safe_component(design_id, "design_id") / f"r{revision}.json"
        if not path.exists():
            return None
        return _parse_model(DesignSpec, json.loads(path.read_text(encoding="utf-8")))

    def put_constraint_artifact(self, constraints: ConstraintSet | Mapping[str, Any], *, name: str,
                                format: Literal["sdc", "xdc"] = "sdc",
                                metadata: Mapping[str, Any] | None = None) -> ArtifactRef:
        """Persist the canonical rendered SDC/XDC and mark its semantic hash.

        Adapters should use this helper after consuming the exact bytes they
        pass to the EDA tool. A later manifest save verifies those bytes.
        """
        contract = constraints if isinstance(constraints, ConstraintSet) else _parse_model(ConstraintSet, constraints)
        content = contract.render(format).encode("utf-8")
        artifact_metadata = dict(metadata or {})
        artifact_metadata.update({"kind": "constraints", "format": format,
                                  "constraints_sha256": contract.content_sha256(),
                                  "renderer": "ConstraintSet.render"})
        return self.put_artifact(content, name=name, media_type="text/plain",
                                 metadata=artifact_metadata)

    def validate_constraint_artifact(self, manifest: RunManifest) -> None:
        """Verify the stored bytes exactly equal the declared ConstraintSet.

        This is deliberately performed at the repository boundary, where the
        content-addressed bytes are available. A hash in a manifest alone is
        not evidence that an adapter consumed the declared constraints.
        """
        artifact = manifest.constraints_artifact
        if artifact is None:
            return
        content = self.get_artifact(artifact.sha256)
        if content is None:
            raise EvidenceError("constraint artifact content is missing")
        if sha256_bytes(content) != artifact.sha256:
            raise EvidenceError("constraint artifact digest does not match its content")
        if len(content) != artifact.size_bytes:
            raise EvidenceError("constraint artifact size does not match its content")
        try:
            contract = _parse_model(ConstraintSet, manifest.constraints)
            format = artifact.metadata.get("format")
            expected = contract.render(format).encode("utf-8")
        except (TypeError, ValueError, KeyError) as exc:
            raise EvidenceError("constraint artifact cannot be validated against declared constraints") from exc
        if content != expected:
            raise EvidenceError("constraint artifact bytes do not match declared constraints")


    def save_manifest(self, manifest: RunManifest) -> str:
        self.validate_constraint_artifact(manifest)
        path = self.runs / f"{self._safe_component(manifest.run_id, 'run_id')}.json"
        immutable = False
        if path.exists():
            existing = _parse_model(RunManifest, json.loads(path.read_text(encoding="utf-8")))
            immutable = existing.status in {"completed", "failed", "cancelled"}
        return self._save_json(path, manifest, immutable=immutable)

    def load_manifest(self, run_id: str) -> RunManifest | None:
        path = self.runs / f"{self._safe_component(run_id, 'run_id')}.json"
        if not path.exists():
            return None
        return _parse_model(RunManifest, json.loads(path.read_text(encoding="utf-8")))

    def save_report(self, report: EvidenceReport) -> str:
        path = self.reports / f"{self._safe_component(report.run_id, 'run_id')}.json"
        return self._save_json(path, report, immutable=False)

    def load_report(self, run_id: str) -> EvidenceReport | None:
        path = self.reports / f"{self._safe_component(run_id, 'run_id')}.json"
        if not path.exists():
            return None
        return _parse_model(EvidenceReport, json.loads(path.read_text(encoding="utf-8")))

    def put_artifact(self, content: bytes, *, name: str,
                     media_type: str = "application/octet-stream",
                     metadata: Mapping[str, Any] | None = None) -> ArtifactRef:
        digest = sha256_bytes(content)
        path = self.artifacts / digest[:2] / digest
        if path.exists() and path.read_bytes() != content:
            raise EvidenceError("content-addressed artifact collision")
        if not path.exists():
            self._atomic_bytes(path, content)
        return ArtifactRef(sha256=digest, name=name, media_type=media_type,
                           size_bytes=len(content), locator=str(path.relative_to(self.root)),
                           metadata=dict(metadata or {}))

    def get_artifact(self, digest: str) -> bytes | None:
        if not _SHA256.fullmatch(digest):
            raise EvidenceError("invalid artifact digest")
        path = self.artifacts / digest[:2] / digest
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def save_yaml(self, path: str | os.PathLike[str], value: BaseModel) -> None:
        try:
            import yaml
        except ImportError as exc:
            raise EvidenceYamlUnavailable("install PyYAML for YAML support") from exc
        target = Path(path).absolute()
        self._atomic_bytes(target, yaml.safe_dump(canonical_data(value), sort_keys=True,
                                                  allow_unicode=True).encode("utf-8"))

    @staticmethod
    def load_yaml(path: str | os.PathLike[str], model_type: type[T]) -> T:
        try:
            import yaml
        except ImportError as exc:
            raise EvidenceYamlUnavailable("install PyYAML for YAML support") from exc
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if not isinstance(data, Mapping):
            raise EvidenceError("YAML evidence document must contain an object")
        return _parse_model(model_type, data)

    def iter_manifests(self) -> Iterator[RunManifest]:
        if not self.runs.is_dir():
            return
        for path in sorted(self.runs.glob("run-*.json")):
            try:
                yield _parse_model(RunManifest, json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue


DEFAULT_EVIDENCE_ROOT = Path(__file__).resolve().parent / "data" / "evidence"
repository = EvidenceRepository(os.environ.get("EVIDENCE_ROOT", DEFAULT_EVIDENCE_ROOT))

