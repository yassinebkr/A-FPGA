"""Fail-closed parsing of routed physical-design check reports.

This module converts OpenROAD/SiliconCompiler text reports into immutable,
hash-bound evidence. Missing or malformed reports fail closed. Waivers name
one exact violation object and never apply globally.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "physical-evidence/v1"
REQUIRED_REPORTS = ("drv_violators", "floating_nets", "overdriven_nets",
                    "unconstrained_endpoints")
_DRV_CATEGORIES = {"max slew": "max_slew", "max capacitance": "max_capacitance",
                   "max cap": "max_capacitance", "max fanout": "max_fanout"}
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_DRV_ROW = re.compile(
    rf"^\s*(?P<object>.+?)\s+(?P<limit>{_NUMBER})\s+"
    rf"(?P<value>{_NUMBER})\s+(?P<slack>{_NUMBER})\s+"
    r"\((?P<state>[A-Za-z_ -]+)\)\s*$")
_DRV_ROW_NO_SLACK = re.compile(
    rf"^\s*(?P<object>.+?)\s+(?P<limit>{_NUMBER})\s+"
    rf"(?P<value>{_NUMBER})\s+"
    r"\((?P<state>[A-Za-z_ -]+)\)\s*$")
_DIVIDER = re.compile(r"^\s*-{3,}\s*$")
_HEADER = re.compile(r"^\s*(?:pin|net|object)\s+(?:limit|name)\b", re.I)
_NO_VIOLATION = re.compile(
    r"^\s*(?:(?:no\s+)?(?:violations?|floating\s+nets?|overdriven\s+nets?|"
    r"unconstrained\s+endpoints?)\s*(?:found|reported|present)?|"
    r"no\s+paths?\s+found\.?)\s*$", re.I)
_UNCONSTRAINED_START = re.compile(r"^\s*Startpoint:\s*(?P<name>.+?)\s*(?:\(|$)", re.I | re.M)
_UNCONSTRAINED_ENDPOINT = re.compile(r"^\s*Endpoint:\s*(?P<name>.+?)\s*(?:\(|$)", re.I | re.M)


class PhysicalEvidenceError(ValueError):
    """Reports cannot support a physical qualification result."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _finite(value: str, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PhysicalEvidenceError(f"invalid {label}: {value!r}") from exc
    if not math.isfinite(result):
        raise PhysicalEvidenceError(f"non-finite {label}: {value!r}")
    return result


@dataclass(frozen=True)
class PhysicalViolation:
    category: str
    object_name: str
    limit: float
    value: float
    slack: float
    status: str = "unwaived"
    waiver_reason: str | None = None

    def canonical_data(self) -> dict[str, Any]:
        return {"category": self.category, "object_name": self.object_name,
                "limit": self.limit, "value": self.value, "slack": self.slack,
                "status": self.status, "waiver_reason": self.waiver_reason}


@dataclass(frozen=True)
class ReportEvidence:
    category: str
    path: str
    sha256: str
    bytes_read: int
    entries: tuple[str, ...] = ()

    def canonical_data(self) -> dict[str, Any]:
        return {"category": self.category, "path": self.path,
                "sha256": self.sha256, "bytes_read": self.bytes_read,
                "entries": list(self.entries)}


@dataclass(frozen=True)
class PhysicalEvidence:
    reports: tuple[ReportEvidence, ...]
    violations: tuple[PhysicalViolation, ...]
    floating_nets: tuple[str, ...]
    overdriven_nets: tuple[str, ...]
    unconstrained_endpoints: tuple[str, ...]
    status: str
    report_digest: str

    @property
    def unresolved_violation_count(self) -> int:
        return sum(v.status == "unwaived" for v in self.violations)

    @property
    def clean(self) -> bool:
        return (self.status == "clean" and not self.floating_nets
                and not self.overdriven_nets and not self.unconstrained_endpoints)

    def canonical_data(self) -> dict[str, Any]:
        return {"schema": SCHEMA_VERSION, "status": self.status,
                "reports": [r.canonical_data() for r in self.reports],
                "violations": [v.canonical_data() for v in self.violations],
                "floating_nets": list(self.floating_nets),
                "overdriven_nets": list(self.overdriven_nets),
                "unconstrained_endpoints": list(self.unconstrained_endpoints),
                "report_digest": self.report_digest}

    def as_dict(self) -> dict[str, Any]:
        return self.canonical_data() | {
            "unresolved_violation_count": self.unresolved_violation_count,
            "clean": self.clean,
        }


def _read_report(category: str, path_value: str | Path) -> tuple[Path, bytes, str]:
    path = Path(path_value)
    if not path.is_file():
        raise PhysicalEvidenceError(f"missing {category} report: {path}")
    try:
        raw = path.read_bytes()
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PhysicalEvidenceError(f"malformed {category} report (not UTF-8): {path}") from exc
    except OSError as exc:
        raise PhysicalEvidenceError(f"cannot read {category} report: {path}") from exc
    return path, raw, _sha256(raw)


def _parse_drv(text: str, report: str) -> tuple[PhysicalViolation, ...]:
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    malformed: list[str] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        heading = _DRV_CATEGORIES.get(stripped.lower())
        if heading:
            current = (heading, [])
            sections.append(current)
            continue
        if not stripped or _DIVIDER.match(line) or _HEADER.match(line):
            continue
        if current is None:
            malformed.append(f"line {line_no}: {stripped}")
            continue
        match = _DRV_ROW.match(line) or _DRV_ROW_NO_SLACK.match(line)
        if not match:
            malformed.append(f"line {line_no}: {stripped}")
            continue
        state = match.group("state").strip().lower()
        if state not in {"violated", "violation"}:
            malformed.append(f"line {line_no}: unsupported status {state!r}")
            continue
        current[1].append(line)
    if malformed:
        raise PhysicalEvidenceError(f"malformed {report} report: " + "; ".join(malformed[:3]))
    violations: list[PhysicalViolation] = []
    for category, rows in sections:
        for row in rows:
            match = _DRV_ROW.match(row) or _DRV_ROW_NO_SLACK.match(row)
            assert match is not None
            object_name = match.group("object").strip()
            if not object_name:
                raise PhysicalEvidenceError(f"malformed {report} report: empty object")
            violations.append(PhysicalViolation(
                category, object_name, _finite(match.group("limit"), "limit"),
                _finite(match.group("value"), "value"),
                (_finite(match.group("slack"), "slack")
                 if "slack" in match.groupdict() else
                 _finite(match.group("limit"), "limit") -
                 _finite(match.group("value"), "value"))))
    return tuple(violations)


def _parse_name_report(text: str, category: str) -> tuple[str, ...]:
    entries: list[str] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or _DIVIDER.match(line) or _NO_VIOLATION.match(line):
            continue
        if stripped.lower() in {category.replace("_", " "), category,
                                "net", "pin", "endpoint", "name"}:
            continue
        if len(stripped) > 512 or "\\x00" in stripped:
            raise PhysicalEvidenceError(f"malformed {category} report at line {line_no}")
        entries.append(stripped)
    return tuple(entries)


def _parse_unconstrained_report(text: str) -> tuple[str, ...]:
    """Return one entry per OpenSTA unconstrained path endpoint.

    OpenSTA emits a complete path report, not a flat endpoint list. Counting
    every non-empty detail line made one path look like over a hundred
    unconstrained endpoints. A plain one-name-per-line report remains
    supported for adapters which already normalize their output.
    """
    starts = _UNCONSTRAINED_START.findall(text)
    endpoints = tuple(name.strip() for name in _UNCONSTRAINED_ENDPOINT.findall(text))
    if starts or endpoints:
        if not starts or not endpoints or len(starts) != len(endpoints):
            raise PhysicalEvidenceError(
                "malformed unconstrained_endpoints report: unmatched Startpoint/Endpoint block")
        return endpoints
    return _parse_name_report(text, "unconstrained_endpoints")


def _validate_waiver(item: Mapping[str, Any]) -> tuple[str, str, str]:
    if not isinstance(item, Mapping):
        raise PhysicalEvidenceError("waiver must be an object")
    category = str(item.get("category", "")).strip()
    object_name = str(item.get("object_name", item.get("object", ""))).strip()
    reason = str(item.get("reason", "")).strip()
    if category not in {"max_slew", "max_capacitance", "max_fanout"}:
        raise PhysicalEvidenceError("waiver category must be max_slew, max_capacitance, or max_fanout")
    if not object_name or object_name in {"*", "**"} or any(ch in object_name for ch in "*?"):
        raise PhysicalEvidenceError("waiver must name one exact object; wildcards are forbidden")
    if not reason or "\\n" in reason or "\\r" in reason:
        raise PhysicalEvidenceError("waiver reason must be a non-empty single line")
    return category, object_name, reason


def collect_physical_evidence(
    report_paths: Mapping[str, str | Path], *,
    waivers: Sequence[Mapping[str, Any]] = (),
    additional_unconstrained_reports: Sequence[str | Path] = (),
) -> PhysicalEvidence:
    """Parse required final physical reports and return hash-bound evidence."""
    missing = [name for name in REQUIRED_REPORTS if name not in report_paths]
    if missing:
        raise PhysicalEvidenceError("missing required report path(s): " + ", ".join(missing))
    report_items: list[ReportEvidence] = []
    violations: list[PhysicalViolation] = []
    named: dict[str, tuple[str, ...]] = {}
    for category in REQUIRED_REPORTS:
        path, raw, digest = _read_report(category, report_paths[category])
        text = raw.decode("utf-8")
        if category == "drv_violators":
            parsed = _parse_drv(text, category)
            violations.extend(parsed)
            entries = tuple(v.object_name for v in parsed)
        else:
            entries = (_parse_unconstrained_report(text)
                       if category == "unconstrained_endpoints"
                       else _parse_name_report(text, category))
            named[category] = entries
        report_items.append(ReportEvidence(category, str(path), digest, len(raw), entries))

    # OpenSTA can disagree with its own check_timing unconstrained metric: the
    # compact report may be empty while report_checks -unconstrained contains
    # real paths. Bind every detailed/corner report into the same evidence.
    extra_unconstrained: list[str] = []
    for index, path_value in enumerate(additional_unconstrained_reports):
        category = f"unconstrained_detail_{index}"
        path, raw, digest = _read_report(category, path_value)
        entries = _parse_unconstrained_report(raw.decode("utf-8"))
        extra_unconstrained.extend(entries)
        report_items.append(ReportEvidence(
            category, str(path), digest, len(raw), entries))

    waiver_keys: dict[tuple[str, str], str] = {}
    for item in waivers:
        category, object_name, reason = _validate_waiver(item)
        key = (category, object_name)
        if key in waiver_keys:
            raise PhysicalEvidenceError(f"duplicate waiver for {category}:{object_name}")
        waiver_keys[key] = reason
    updated: list[PhysicalViolation] = []
    seen_keys: set[tuple[str, str]] = set()
    for violation in violations:
        key = (violation.category, violation.object_name)
        reason = waiver_keys.get(key)
        seen_keys.add(key)
        updated.append(PhysicalViolation(
            violation.category, violation.object_name, violation.limit,
            violation.value, violation.slack, "waived" if reason else "unwaived",
            reason))
    unmatched = set(waiver_keys) - seen_keys
    if unmatched:
        category, object_name = sorted(unmatched)[0]
        raise PhysicalEvidenceError(f"waiver does not match a reported violation: {category}:{object_name}")

    floating = named.get("floating_nets", ())
    overdriven = named.get("overdriven_nets", ())
    unconstrained = tuple(dict.fromkeys(
        (*named.get("unconstrained_endpoints", ()), *extra_unconstrained)))
    status = "clean" if not (any(v.status == "unwaived" for v in updated)
                             or floating or overdriven or unconstrained) else "violations"
    canonical = {"schema": SCHEMA_VERSION,
                 "reports": [r.canonical_data() for r in report_items],
                 "violations": [v.canonical_data() for v in updated],
                 "floating_nets": list(floating),
                 "overdriven_nets": list(overdriven),
                 "unconstrained_endpoints": list(unconstrained)}
    digest = _sha256(json.dumps(canonical, sort_keys=True,
                                separators=(",", ":"), ensure_ascii=False).encode())
    return PhysicalEvidence(tuple(report_items), tuple(updated), floating,
                            overdriven, unconstrained, status, digest)


parse_physical_evidence = collect_physical_evidence

__all__ = ["SCHEMA_VERSION", "REQUIRED_REPORTS", "PhysicalEvidenceError",
           "PhysicalViolation", "ReportEvidence", "PhysicalEvidence",
           "collect_physical_evidence", "parse_physical_evidence"]
