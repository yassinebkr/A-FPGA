"""Project-owned verification policy and evidence helpers.

This module intentionally has no dependency on a particular HDL tool.  It
normalizes linter diagnostics to stable rule ids, applies auditable waivers,
and provides small schemas for assertion and functional-coverage evidence.
The input accepted by ``*_from_design_spec`` is deliberately duck-typed so it
can consume the eventual DesignSpec model without importing it (and without
creating a dependency cycle with the run/evidence store).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from datetime import date
from enum import Enum
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "verification-evidence/v1"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class RuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    description: str
    default_severity: Severity = Severity.WARNING
    aliases: tuple[str, ...] = ()


# Rule ids are owned by this project rather than by whichever linter happens
# to be installed.  Tool codes remain in the diagnostic's ``code`` field.
DEFAULT_RULES: dict[str, RuleSpec] = {
    "RTL-SYNTAX": RuleSpec(
        id="RTL-SYNTAX", category="syntax",
        description="RTL must parse as the requested HDL dialect.",
        default_severity=Severity.ERROR,
        aliases=("syntax", "expectedtoken", "parse"),
    ),
    "RTL-ELAB": RuleSpec(
        id="RTL-ELAB", category="elaboration",
        description="RTL must elaborate with a valid top-level design.",
        default_severity=Severity.ERROR,
        aliases=("elab", "missing", "notfound", "undeclared"),
    ),
    "RTL-WIDTH": RuleSpec(
        id="RTL-WIDTH", category="width",
        description="Implicit width/sign conversions must be intentional.",
        aliases=("width", "widthexpand", "widthtrunc", "widthcast"),
    ),
    "RTL-LATCH": RuleSpec(
        id="RTL-LATCH", category="latch",
        description="Combinational logic must not infer unintended latches.",
        aliases=("latch", "always_comb"),
    ),
    "RTL-CLOCK": RuleSpec(
        id="RTL-CLOCK", category="clock_reset",
        description="Clock and reset usage must follow the project policy.",
        aliases=("clock", "reset", "async", "syncasyncnet"),
    ),
    "RTL-SAFETY": RuleSpec(
        id="RTL-SAFETY", category="synthesis_safety",
        description="RTL must avoid constructs unsafe or ambiguous for synthesis.",
        aliases=("unsynth", "synthesis", "initial", "multidriven", "case"),
    ),
    "RTL-PORTABILITY": RuleSpec(
        id="RTL-PORTABILITY", category="portability",
        description="RTL should remain portable across supported toolchains.",
        aliases=("portability", "unsupported", "deprecated", "declfilename"),
    ),
    "RTL-STYLE": RuleSpec(
        id="RTL-STYLE", category="style",
        description="RTL style findings must be understood and policy-compliant.",
        aliases=("style", "unused", "name"),
    ),
    "RTL-OTHER": RuleSpec(
        id="RTL-OTHER", category="other",
        description="Unclassified linter findings require review.",
        default_severity=Severity.ERROR,
    ),
}


# Thresholds are explicit by design class.  A design may override these in a
# DesignSpec, but there is no silent global default.
DEFAULT_COVERAGE_THRESHOLDS: dict[str, float] = {
    "combinational": 0.90,
    "sequential": 0.95,
    "protocol": 0.95,
    "memory": 0.90,
    "arithmetic": 0.95,
    "mixed": 0.95,
    "unknown": 0.90,
}


class WaiverScope(BaseModel):
    """Where a waiver is valid; omitted fields mean any value."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str | None = None
    top_module: str | None = None
    design_class: str | None = None
    file_glob: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_lines(self) -> "WaiverScope":
        if self.line_start is not None and self.line_end is not None and self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        if not any((self.rule_id, self.top_module, self.design_class, self.file_glob,
                    self.line_start is not None, self.line_end is not None)):
            raise ValueError("waiver scope must constrain at least one field")
        return self


class LintWaiver(BaseModel):
    """An auditable, expiring exception to the production lint policy."""

    model_config = ConfigDict(extra="forbid")

    id: str
    reason: str = Field(min_length=1)
    author: str = Field(min_length=1)
    scope: WaiverScope
    expires: date
    ticket: str | None = None

    @field_validator("id", "author", "reason")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    def active(self, *, on: date | None = None) -> bool:
        return self.expires >= (on or date.today())


class LintPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "lint-policy/v1"
    design_class: str = "unknown"
    rules: dict[str, Severity] = Field(default_factory=dict)
    waivers: list[LintWaiver] = Field(default_factory=list)
    coverage_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_rule_ids(self) -> "LintPolicy":
        unknown = set(self.rules) - set(DEFAULT_RULES)
        if unknown:
            raise ValueError(f"unknown lint rule id(s): {', '.join(sorted(unknown))}")
        return self

    def severity_for(self, rule_id: str) -> Severity:
        return self.rules.get(rule_id, DEFAULT_RULES[rule_id].default_severity)

    def threshold(self) -> float:
        if self.coverage_threshold is not None:
            return self.coverage_threshold
        try:
            return DEFAULT_COVERAGE_THRESHOLDS[self.design_class]
        except KeyError as exc:
            raise ValueError(
                f"no coverage threshold configured for design class {self.design_class!r}"
            ) from exc


class AssertionProperty(BaseModel):
    """One requirement-backed SystemVerilog assertion or cover property."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    expression: str = Field(min_length=1)
    kind: str = "assert"
    clock: str | None = None
    disable_iff: str | None = None
    requirement_id: str | None = None
    source: str = "design_spec"

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        if value not in {"assert", "assume", "cover"}:
            raise ValueError("assertion kind must be assert, assume, or cover")
        return value


class AssertionResult(BaseModel):
    property_id: str
    status: str
    attempts: int = Field(default=1, ge=0)
    counterexample_artifact: str | None = None
    tool: str | None = None
    message: str | None = None

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in {"proven", "failed", "bounded", "inconclusive", "not-applicable"}:
            raise ValueError("invalid assertion result status")
        return value


class CoverageRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str = "functional"
    target: str
    threshold: float = Field(ge=0.0, le=1.0)
    mandatory: bool = True
    requirement_id: str | None = None
    field: str | None = None
    bins: list[Any] = Field(default_factory=list)
    cross: list[str] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        if value not in {"functional", "code", "cross", "assertion"}:
            raise ValueError("invalid coverage kind")
        return value


class CoverageResult(BaseModel):
    requirement_id: str
    covered: int = Field(ge=0)
    total: int = Field(ge=0)
    percentage: float = Field(ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    passed: bool
    source: str = "coverage_tool"

    @model_validator(mode="after")
    def consistent(self) -> "CoverageResult":
        expected = self.covered / self.total if self.total else 0.0
        if abs(expected - self.percentage) > 1e-6:
            raise ValueError("percentage must equal covered / total")
        return self

    @classmethod
    def from_counts(cls, requirement_id: str, covered: int, total: int,
                    threshold: float, *, source: str = "coverage_tool") -> "CoverageResult":
        percentage = covered / total if total else 0.0
        return cls(requirement_id=requirement_id, covered=covered, total=total,
                   percentage=percentage, threshold=threshold,
                   passed=percentage >= threshold, source=source)


class LintEvidence(BaseModel):
    ok: bool
    unwaived_errors: int = Field(ge=0)
    waived_errors: int = Field(ge=0)
    warnings: int = Field(ge=0)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    waivers_applied: list[str] = Field(default_factory=list)


class VerificationEvidence(BaseModel):
    """Stable, serializable evidence object suitable for a RunManifest."""

    schema_version: str = SCHEMA_VERSION
    design_class: str
    lint: LintEvidence
    assertions: list[AssertionResult] = Field(default_factory=list)
    coverage: list[CoverageResult] = Field(default_factory=list)
    coverage_threshold: float
    gates: dict[str, bool] = Field(default_factory=dict)
    overall_pass: bool

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    return json.dumps(value, sort_keys=True, default=str)


def canonical_rule_id(code: Any, message: Any = "") -> str:
    """Map tool-specific code/message pairs to stable project rule ids."""
    haystack = f"{code or ''} {_text(message)}".lower()
    for rule_id, rule in DEFAULT_RULES.items():
        if any(alias.lower() in haystack for alias in rule.aliases):
            return rule_id
    if re.search(r"syntax|unexpected|malformed|parse", haystack):
        return "RTL-SYNTAX"
    if re.search(r"cannot find|missing|undeclared|elaborat", haystack):
        return "RTL-ELAB"
    return "RTL-OTHER"


def _scope_matches(waiver: LintWaiver, diagnostic: Mapping[str, Any], *,
                   top_module: str | None, design_class: str) -> bool:
    scope = waiver.scope
    rule_id = diagnostic.get("rule_id") or canonical_rule_id(diagnostic.get("code"), diagnostic.get("msg"))
    if scope.rule_id and scope.rule_id != rule_id:
        return False
    if scope.top_module and scope.top_module != top_module:
        return False
    if scope.design_class and scope.design_class != design_class:
        return False
    file_name = str(diagnostic.get("file") or "")
    if scope.file_glob and not fnmatch.fnmatch(file_name, scope.file_glob):
        return False
    line = diagnostic.get("line")
    if scope.line_start is not None and (line is None or line < scope.line_start):
        return False
    if scope.line_end is not None and (line is None or line > scope.line_end):
        return False
    return True


def apply_lint_policy(report: Mapping[str, Any], policy: LintPolicy, *,
                      top_module: str | None = None, on: date | None = None) -> dict[str, Any]:
    """Apply severity overrides and active waivers to a lint report.

    The existing report keys are retained. New fields are additive, allowing
    old clients to keep consuming ``ok``/``verdict``/``errors``.
    """
    out = dict(report)
    diagnostics = []
    unwaived = waived = warnings = 0
    applied: list[str] = []
    for original in report.get("errors", []):
        diagnostic = dict(original)
        rule_id = canonical_rule_id(diagnostic.get("code"), diagnostic.get("msg"))
        configured = policy.severity_for(rule_id)
        diagnostic["rule_id"] = rule_id
        diagnostic["policy_severity"] = configured.value
        if configured is Severity.INFO:
            diagnostic["policy_ignored"] = True
            warnings += 1
            diagnostics.append(diagnostic)
            continue
        active = [w for w in policy.waivers if w.active(on=on) and _scope_matches(
            w, diagnostic, top_module=top_module, design_class=policy.design_class)]
        if active:
            diagnostic["waived"] = True
            diagnostic["waiver_id"] = active[0].id
            applied.append(active[0].id)
            waived += 1
        elif configured is Severity.ERROR:
            # The project policy, not a vendor's default warning/error choice,
            # decides whether a finding blocks the gate.
            unwaived += 1
        else:
            warnings += 1
        diagnostics.append(diagnostic)
    out["errors"] = diagnostics
    # A linter that failed without producing diagnostics is still a failed
    # gate; otherwise a crashed/unavailable parser could be mistaken for a
    # clean design. An explicitly waived diagnostic failure may pass.
    unparsed_failure = bool(not report.get("ok", False) and not diagnostics)
    passed = unwaived == 0 and not unparsed_failure
    out["policy"] = {
        "schema_version": "lint-policy/v1",
        "design_class": policy.design_class,
        "unwaived_errors": unwaived,
        "waived_errors": waived,
        "warnings": warnings,
        "waivers_applied": sorted(set(applied)),
        "passed": passed,
        "unparsed_failure": unparsed_failure,
    }
    # Production policy, not the raw linter, owns the final gate.
    out["ok"] = passed
    return out


def _spec_mapping(spec: Any) -> Mapping[str, Any]:
    if isinstance(spec, Mapping):
        return spec
    if hasattr(spec, "model_dump"):
        return spec.model_dump(mode="python")
    if hasattr(spec, "__dict__"):
        return vars(spec)
    return {}


def _verification_section(spec: Any) -> Mapping[str, Any]:
    data = _spec_mapping(spec)
    section = data.get("verification", {})
    return section if isinstance(section, Mapping) else {}


def assertions_from_design_spec(spec: Any) -> list[AssertionProperty]:
    """Extract explicit property declarations from a DesignSpec-like value."""
    data = _spec_mapping(spec)
    section = _verification_section(spec)
    raw_assertions = section.get("assertions", data.get("assertions", []))
    raw_properties = section.get("properties", data.get("properties", []))
    raw: list[Any] = []
    for group in (raw_assertions, raw_properties):
        if isinstance(group, Mapping):
            raw.extend(group.values())
        elif isinstance(group, Iterable) and not isinstance(group, (str, bytes)):
            raw.extend(group)
    result: list[AssertionProperty] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            item = {"name": f"property_{index}", "expression": item}
        if not isinstance(item, Mapping):
            continue
        expression = item.get("expression", item.get("property"))
        if not expression:
            continue
        name = str(item.get("name", f"property_{index}"))
        ident = str(item.get("id") or "assert-" + hashlib.sha256(
            f"{name}:{expression}".encode()).hexdigest()[:12])
        result.append(AssertionProperty(
            id=ident, name=name, expression=str(expression),
            kind=str(item.get("kind", "assert")), clock=item.get("clock"),
            disable_iff=item.get("disable_iff"), requirement_id=item.get("requirement_id"),
            source=str(item.get("source", "design_spec")),
        ))
    return result


_SVA_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_SVA_KEYWORDS = {
    "assert", "assume", "cover", "property", "sequence", "disable", "iff",
    "posedge", "negedge", "and", "or", "not", "until", "throughout", "within",
    "s_until", "s_eventually", "strong", "weak", "first_match", "true", "false",
}
_SVA_SYSTEM_NAMES = {"past", "rose", "fell", "stable", "changed", "onehot", "isunknown"}


def _infer_sva_ports(properties: list[AssertionProperty]) -> dict[str, Any]:
    names: set[str] = set()
    for prop in properties:
        names.update(_SVA_IDENT.findall(prop.expression))
        if prop.clock:
            names.add(prop.clock)
        if prop.disable_iff:
            names.update(_SVA_IDENT.findall(prop.disable_iff))
    return {
        name: 1 for name in sorted(names)
        if name not in _SVA_KEYWORDS and name not in _SVA_SYSTEM_NAMES
    }


def render_sva(properties: Iterable[AssertionProperty], *, module_name: str = "agent_properties",
               bind_target: str | None = None, ports: Mapping[str, Any] | None = None) -> str:
    """Render a syntactically meaningful checker module.

    The generated checker declares read-only ports, inferred as scalar signals
    when no port map is supplied. For a real design, pass ``ports`` from the
    DesignSpec interfaces and ``bind_target`` to append a legal SystemVerilog
    ``bind`` statement. Without ``bind_target`` this is a standalone checker
    artifact; it is not represented as already attached to the DUT.
    """
    props = list(properties)
    port_map = dict(ports or _infer_sva_ports(props))
    lines = [f"// Generated verification properties; checker: {module_name}"]
    if port_map:
        lines.append(f"module {module_name} (")
        declarations = []
        for name, width in port_map.items():
            if isinstance(width, int):
                declaration = "input logic" if width <= 1 else f"input logic [{width - 1}:0]"
            else:
                declaration = str(width)
                if not declaration.startswith("input"):
                    declaration = "input " + declaration
            declarations.append(f"    {declaration} {name}")
        lines.append(",\n".join(declarations))
        lines.append(");")
    else:
        lines.append(f"module {module_name};")
    for prop in props:
        disable = f" disable iff ({prop.disable_iff})" if prop.disable_iff else ""
        event = f"@(posedge {prop.clock})" if prop.clock else ""
        prefix = f"{event}{disable} " if event or disable else ""
        lines.append(f"  {prop.name}: {prop.kind} property ({prefix}{prop.expression});")
    lines.append("endmodule")
    if bind_target:
        connections = ", ".join(f".{name}({name})" for name in port_map)
        lines.append(f"bind {bind_target} {module_name} {module_name}_bound ({connections});")
    return "\n".join(lines) + "\n"


def render_sva_for_design_spec(spec: Any, *, module_name: str | None = None,
                               bind_target: str | None = None) -> str:
    """Render a checker using DesignSpec interfaces/clock ports as its port map."""
    data = _spec_mapping(spec)
    props = assertions_from_design_spec(spec)
    ports: dict[str, Any] = {}
    for item in data.get("interfaces", []):
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="python")
        if not isinstance(item, Mapping) or not item.get("name"):
            continue
        ports[str(item["name"])] = int(item.get("width", 1))
    for item in data.get("clock_domains", []):
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="python")
        if isinstance(item, Mapping) and item.get("clock_port"):
            ports.setdefault(str(item["clock_port"]), 1)
            if item.get("reset_port"):
                ports.setdefault(str(item["reset_port"]), 1)
    return render_sva(props, module_name=module_name or "agent_properties",
                      bind_target=bind_target or data.get("top_module"), ports=ports)


def coverage_requirements_from_design_spec(spec: Any, *, design_class: str | None = None) -> list[CoverageRequirement]:
    data = _spec_mapping(spec)
    section = _verification_section(spec)
    klass = design_class or str(data.get("design_class", section.get("design_class", "unknown")))
    raw = section.get("coverage_requirements", section.get("coverage", data.get("coverage_requirements", [])))
    class_threshold = DEFAULT_COVERAGE_THRESHOLDS.get(klass, DEFAULT_COVERAGE_THRESHOLDS["unknown"])
    # The initial DesignSpec model expresses this as a percentage on its
    # VerificationPlan; accept that shape while keeping the policy's internal
    # representation in [0, 1].
    if section.get("coverage_target_percent") is not None:
        class_threshold = float(section["coverage_target_percent"]) / 100.0
    result: list[CoverageRequirement] = []
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes)):
        for index, item in enumerate(raw):
            if isinstance(item, str):
                item = {"id": f"functional-{index}", "target": item}
            if not isinstance(item, Mapping) or not item.get("target"):
                continue
            result.append(CoverageRequirement(
                id=str(item.get("id", f"coverage-{index}")),
                kind=str(item.get("kind", "functional")), target=str(item["target"]),
                threshold=float(class_threshold if item.get("threshold") is None else item["threshold"]),
                mandatory=bool(item.get("mandatory", True)), requirement_id=item.get("requirement_id"),
                field=item.get("field") or item.get("sample_field"),
                bins=list(item.get("bins", [])), cross=list(item.get("cross", item.get("dimensions", []))),
            ))
    if result:
        return result
    # Evidence DesignSpec revisions model functional requirements separately.
    # Promote those requirements into coverage obligations when no explicit
    # coverage list exists; this prevents a generic synthetic target from
    # hiding missing requirement coverage.
    requirements = data.get("requirements", [])
    for item in requirements if isinstance(requirements, Iterable) else []:
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="python")
        if isinstance(item, Mapping) and item.get("kind", "functional") in {"functional", "verification"}:
            result.append(CoverageRequirement(
                id=str(item.get("id")), kind="functional", target=str(item.get("text", item.get("id"))),
                threshold=class_threshold, mandatory=bool(item.get("mandatory", True)),
                requirement_id=str(item.get("id")),
            ))
    if result:
        return result
    return [CoverageRequirement(id=f"{klass}-functional", kind="functional",
                                target="required functional scenarios",
                                threshold=class_threshold)]


def evaluate_coverage(requirements: Iterable[CoverageRequirement], results: Iterable[CoverageResult], *,
                      design_class: str = "unknown") -> dict[str, Any]:
    reqs = list(requirements)
    result_list = list(results)
    by_id = {item.requirement_id: item for item in result_list}
    mandatory = {r.id for r in reqs if r.mandatory}
    missing = [item.id for item in reqs if item.mandatory and item.id not in by_id]
    failed = []
    for item in result_list:
        requirement = next((req for req in reqs if req.id == item.requirement_id), None)
        if requirement and requirement.mandatory and (
                not item.passed or item.percentage < requirement.threshold):
            failed.append(item.requirement_id)
    return {"design_class": design_class,
            "threshold": DEFAULT_COVERAGE_THRESHOLDS.get(design_class, DEFAULT_COVERAGE_THRESHOLDS["unknown"]),
            "required": [r.id for r in reqs], "missing": missing,
            "failed": sorted(set(failed) & mandatory),
            "passed": not missing and not failed}


def build_verification_evidence(*, design_class: str, lint_report: Mapping[str, Any],
                                assertions: Iterable[AssertionResult] = (),
                                coverage: Iterable[CoverageResult] = (),
                                coverage_threshold: float | None = None) -> VerificationEvidence:
    lint_policy = lint_report.get("policy") or {}
    lint_ev = LintEvidence(
        ok=bool(lint_policy.get("passed", lint_report.get("ok", False))),
        unwaived_errors=int(lint_policy.get("unwaived_errors", lint_report.get("errors_total", 0))),
        waived_errors=int(lint_policy.get("waived_errors", 0)),
        warnings=int(lint_policy.get("warnings", lint_report.get("warnings_total", 0))),
        diagnostics=list(lint_report.get("errors", [])),
        waivers_applied=list(lint_policy.get("waivers_applied", [])),
    )
    assertion_results = list(assertions)
    coverage_results = list(coverage)
    threshold = coverage_threshold if coverage_threshold is not None else DEFAULT_COVERAGE_THRESHOLDS.get(design_class, DEFAULT_COVERAGE_THRESHOLDS["unknown"])
    assertions_ok = all(item.status in {"proven", "not-applicable"} for item in assertion_results)
    coverage_ok = bool(coverage_results) and all(
        item.passed and item.percentage >= threshold for item in coverage_results)
    gates = {"lint": lint_ev.ok, "assertions": assertions_ok, "functional_coverage": coverage_ok}
    return VerificationEvidence(design_class=design_class, lint=lint_ev,
                                assertions=assertion_results, coverage=coverage_results,
                                coverage_threshold=threshold, gates=gates,
                                overall_pass=all(gates.values()))


# Public convenience names used by callers that do not need the longer model
# names. Keeping aliases here also makes policy files pleasant to read.
Waiver = LintWaiver
SVAssertion = AssertionProperty

def lint_policy_from_design_spec(spec: Any) -> LintPolicy:
    data = _spec_mapping(spec)
    section = _verification_section(spec)
    design_class = str(data.get("design_class", section.get("design_class", "unknown")))
    raw_rules = section.get("lint_rules", data.get("lint_rules", {}))
    if isinstance(raw_rules, list):
        raw_rules = {str(item["id"]): item.get("severity", "warning")
                     for item in raw_rules if isinstance(item, Mapping) and item.get("id")}
    raw_waivers = section.get("waivers", data.get("waivers", []))
    waivers = [LintWaiver.model_validate(item) for item in raw_waivers]
    threshold = section.get("coverage_threshold", data.get("coverage_threshold"))
    if threshold is None and section.get("coverage_target_percent") is not None:
        threshold = float(section["coverage_target_percent"]) / 100.0
    return LintPolicy(design_class=design_class, rules=raw_rules or {},
                      waivers=waivers, coverage_threshold=threshold)


def derive_assertions(spec: Any) -> list[AssertionProperty]:
    return assertions_from_design_spec(spec)


def derive_coverage_requirements(spec: Any, *, design_class: str | None = None) -> list[CoverageRequirement]:
    return coverage_requirements_from_design_spec(spec, design_class=design_class)


def evaluate_lint(report: Mapping[str, Any], policy: LintPolicy, **kwargs: Any) -> dict[str, Any]:
    return apply_lint_policy(report, policy, **kwargs)


def generate_sva(properties: Iterable[AssertionProperty], *, module_name: str = "agent_properties") -> str:
    return render_sva(properties, module_name=module_name)
