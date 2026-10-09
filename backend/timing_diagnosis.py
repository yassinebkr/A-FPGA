"""Small, defensive parser for synthesis/static-timing evidence.

SiliconCompiler exposes scalar values in its package manifest, while the
useful answer to a timing failure lives in OpenSTA's report files.  This
module deliberately has no SiliconCompiler or asyncio dependency: it can be
used by the EDA runner, repair policy, and tests independently.

The public result is a JSON-compatible mapping with canonical units:

``setup_slack_ns`` / ``hold_slack_ns`` / ``clock_period_ns`` are nanoseconds,
``fmax_mhz`` is MHz, and ``paths`` contains the first/worst reported paths.
Missing reports are represented by ``None``/an empty list rather than guessed
values.  Report formats vary a little across OpenSTA versions, so all
parsers are intentionally tolerant and never raise on malformed text.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_NUMBER_RE = re.compile(_NUMBER)


def _finite_float(value: Any) -> float | None:
    """Return a finite float from a number or a string containing one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        match = _NUMBER_RE.search(value.replace(",", "").replace("_", ""))
        if not match:
            return None
        try:
            result = float(match.group(0))
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _metric_value(value: Any, *, unit: str | None = None) -> float | None:
    """Normalize a metric value to the requested canonical unit.

    SiliconCompiler normally returns a string such as ``"-2.43 ns"``.  The
    helper also accepts plain numbers and common Hz/MHz spellings so callers
    can pass either manifest values or already parsed values.
    """
    number = _finite_float(value)
    if number is None:
        return None
    text = str(value).lower() if isinstance(value, str) else ""
    if unit == "mhz" and ("ghz" in text):
        number *= 1000.0
    elif unit == "mhz" and ("hz" in text and "mhz" not in text):
        number /= 1_000_000.0
    elif unit == "ns" and ("ps" in text and "ns" not in text):
        number /= 1000.0
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class TimingPath:
    """One OpenSTA path summary, with optional fields omitted by reports."""

    startpoint: str | None = None
    endpoint: str | None = None
    analysis_type: str = "unknown"  # setup, hold, or unknown
    slack_ns: float | None = None
    delay_ns: float | None = None
    path_group: str | None = None
    clock: str | None = None
    path_type: str | None = None  # max/min as emitted by OpenSTA
    corner: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value for key, value in {
                "startpoint": self.startpoint,
                "endpoint": self.endpoint,
                "analysis_type": self.analysis_type,
                "slack_ns": self.slack_ns,
                "delay_ns": self.delay_ns,
                "path_group": self.path_group,
                "clock": self.clock,
                "path_type": self.path_type,
                "corner": self.corner,
            }.items() if value is not None
        }


@dataclass
class TimingDiagnosis:
    """Canonical timing diagnosis assembled from metrics and reports."""

    setup_slack_ns: float | None = None
    hold_slack_ns: float | None = None
    fmax_mhz: float | None = None
    clock_period_ns: float | None = None
    clock: str | None = None
    paths: list[TimingPath] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    status: str = "unavailable"

    def as_dict(self) -> dict[str, Any]:
        paths = [path.as_dict() for path in self.paths]
        return {
            "status": self.status,
            "setup_slack_ns": self.setup_slack_ns,
            "hold_slack_ns": self.hold_slack_ns,
            "fmax_mhz": self.fmax_mhz,
            "clock_period_ns": self.clock_period_ns,
            "clock": self.clock,
            "paths": paths,
            "source_files": list(self.source_files),
        }


def normalize_timing_metrics(metrics: Mapping[str, Any] | None) -> dict[str, float | None]:
    """Map SC/frontend names to canonical timing names and units.

    ``setupslack``/``wns_ns`` and ``holdslack`` are accepted in addition to
    the canonical names.  For duplicate aliases the canonical key wins when
    it contains a valid value; otherwise the first valid alias is used.
    """
    metrics = metrics or {}

    def pick(names: tuple[str, ...], unit: str) -> float | None:
        for name in names:
            if name in metrics:
                value = _metric_value(metrics[name], unit=unit)
                if value is not None:
                    return value
        # Be forgiving of case differences from external manifests.
        lowered = {str(key).lower(): value for key, value in metrics.items()}
        for name in names:
            if name.lower() in lowered:
                value = _metric_value(lowered[name.lower()], unit=unit)
                if value is not None:
                    return value
        return None

    return {
        "setup_slack_ns": pick(("setup_slack_ns", "setupslack", "wns_ns", "wns"), "ns"),
        "hold_slack_ns": pick(("hold_slack_ns", "holdslack"), "ns"),
        "fmax_mhz": pick(("fmax_mhz", "fmax"), "mhz"),
        "clock_period_ns": pick(("clock_period_ns", "period_ns", "period"), "ns"),
    }


def _analysis_type(path_type: str | None, hint: str | None) -> str:
    if hint in {"setup", "hold"}:
        return hint
    if path_type == "max":
        return "setup"
    if path_type == "min":
        return "hold"
    return "unknown"


def parse_timing_report(text: str, analysis_type: str | None = None) -> list[TimingPath]:
    """Parse one or more OpenSTA ``report_checks`` path blocks.

    A report can contain many paths.  Every block beginning with ``Startpoint``
    is parsed independently; malformed blocks are skipped rather than making
    an otherwise useful synthesis result unavailable.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    blocks = re.split(r"(?m)(?=^Startpoint:\s*)", text)
    paths: list[TimingPath] = []
    for block in blocks:
        if not re.search(r"(?m)^Startpoint:\s*", block):
            continue

        def field(name: str) -> str | None:
            match = re.search(rf"(?im)^\s*{name}:\s*(.+?)\s*$", block)
            return match.group(1).strip() if match else None

        startpoint = field("Startpoint")
        if startpoint and " (" in startpoint:
            startpoint = startpoint.split(" (", 1)[0].strip()
        endpoint = field("Endpoint")
        if endpoint and " (" in endpoint:
            endpoint = endpoint.split(" (", 1)[0].strip()
        path_group = field("Path Group")
        path_type = field("Path Type")
        corner = field("Corner")
        # Clock names are usually in the explanatory start/endpoint text or
        # in Path Group.  Keep the plain group as a useful fallback.
        clock = None
        clock_match = re.search(r"clocked by\s+([^\)\s]+)", block, re.IGNORECASE)
        if clock_match:
            clock = clock_match.group(1)
        elif path_group:
            clock = path_group

        slack = None
        slack_match = re.search(
            rf"(?im)^\s*({_NUMBER})\s+slack\s*\((?:MET|VIOLATED|UNCONSTRAINED)[^\)]*\)",
            block,
        )
        if slack_match:
            slack = _finite_float(slack_match.group(1))
        # Some OpenSTA versions omit the parenthesized status.
        if slack is None:
            slack_match = re.search(rf"(?im)^\s*({_NUMBER})\s+slack\b", block)
            if slack_match:
                slack = _finite_float(slack_match.group(1))

        delay = None
        arrival = re.findall(rf"(?im)^\s*({_NUMBER})\s+data arrival time\s*$", block)
        if arrival:
            delay = _finite_float(arrival[-1])
        kind = _analysis_type(path_type.lower() if path_type else None, analysis_type)
        paths.append(TimingPath(startpoint, endpoint, kind, slack, delay,
                                path_group, clock, path_type, corner))
    return paths


def parse_worst_slack_report(text: str, analysis_type: str | None = None) -> dict[str, float | None]:
    """Parse OpenSTA's one-line ``worst_slack.*.rpt`` report."""
    result: dict[str, float | None] = {"setup_slack_ns": None, "hold_slack_ns": None}
    for match in re.finditer(rf"(?im)\bworst\s+slack\s+(max|min)\s+({_NUMBER})", text or ""):
        kind = analysis_type or ("setup" if match.group(1).lower() == "max" else "hold")
        key = "setup_slack_ns" if kind == "setup" else "hold_slack_ns"
        result[key] = _finite_float(match.group(2))
    return result


def parse_fmax_report(text: str) -> dict[str, float | str | None]:
    """Parse ``reports/clocks/fmax.rpt`` from OpenSTA/SiliconCompiler."""
    result: dict[str, float | str | None] = {
        "clock": None, "clock_period_ns": None, "fmax_mhz": None,
    }
    for line in (text or "").splitlines():
        # Typical report: clock period min_period fmax_mhz registers.
        match = re.match(rf"^\s*(\S+)\s+({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s+\d+\s*$", line)
        if match and match.group(1).lower() not in {"clock", "-"}:
            result.update({"clock": match.group(1),
                           "clock_period_ns": _finite_float(match.group(2)),
                           "fmax_mhz": _finite_float(match.group(4))})
            continue
        match = re.search(rf"(?i)\bfmax\s*=\s*({_NUMBER})\s*(mhz|ghz|hz)?", line)
        if match:
            result["fmax_mhz"] = _metric_value(
                f"{match.group(1)} {match.group(2) or 'MHz'}", unit="mhz")
    return result


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except (OSError, UnicodeError):
        return ""


def read_timing_diagnosis(build_dir: str | Path, top_module: str,
                          metrics: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read SC/OpenSTA reports below ``build_dir`` and return a stable dict.

    ``build_dir`` may be either the design directory (``.../top``) or its
    parent; both forms are used by older callers.  Only files whose names are
    recognizable timing reports are opened.
    """
    root = Path(build_dir)
    if not root.exists() or not root.is_dir():
        return TimingDiagnosis(**normalize_timing_metrics(metrics)).as_dict()

    normalized = normalize_timing_metrics(metrics)
    diagnosis = TimingDiagnosis(**normalized)
    candidates: list[Path] = []
    try:
        for path in root.rglob("*"):
            if path.is_file() and path.name.lower() in {
                "setup.topn.rpt", "setup.failing.rpt", "setup.rpt",
                "hold.topn.rpt", "hold.failing.rpt", "hold.rpt",
                "worst_slack.setup.rpt", "worst_slack.hold.rpt", "fmax.rpt",
            }:
                candidates.append(path)
    except OSError:
        candidates = []

    # Process deterministic order, with fmax/worst-slack reports before path
    # reports so scalar values from the dedicated summary files win.
    candidates.sort(key=lambda item: (0 if "worst_slack" in item.name else
                                      1 if item.name == "fmax.rpt" else 2,
                                      str(item)))
    seen_paths: set[tuple[Any, ...]] = set()
    for path in candidates:
        text = _read_text(path)
        if not text:
            continue
        lower = path.name.lower()
        diagnosis.source_files.append(str(path))
        if lower == "fmax.rpt":
            parsed = parse_fmax_report(text)
            if parsed.get("fmax_mhz") is not None:
                diagnosis.fmax_mhz = float(parsed["fmax_mhz"])
            if parsed.get("clock_period_ns") is not None:
                diagnosis.clock_period_ns = float(parsed["clock_period_ns"])
            if parsed.get("clock"):
                diagnosis.clock = str(parsed["clock"])
            continue
        hint = "hold" if "hold" in lower else "setup"
        worst = parse_worst_slack_report(text, hint)
        if worst["setup_slack_ns"] is not None:
            diagnosis.setup_slack_ns = float(worst["setup_slack_ns"])
        if worst["hold_slack_ns"] is not None:
            diagnosis.hold_slack_ns = float(worst["hold_slack_ns"])
        for path_summary in parse_timing_report(text, hint):
            key = (path_summary.startpoint, path_summary.endpoint,
                   path_summary.analysis_type, path_summary.slack_ns)
            if key not in seen_paths:
                seen_paths.add(key)
                diagnosis.paths.append(path_summary)
            if path_summary.slack_ns is not None:
                field_name = ("hold_slack_ns" if path_summary.analysis_type == "hold"
                              else "setup_slack_ns" if path_summary.analysis_type == "setup"
                              else None)
                if field_name and (getattr(diagnosis, field_name) is None or
                                    path_summary.slack_ns < getattr(diagnosis, field_name)):
                    setattr(diagnosis, field_name, path_summary.slack_ns)
            if diagnosis.clock is None and path_summary.clock:
                diagnosis.clock = path_summary.clock

    diagnosis.source_files = sorted(set(diagnosis.source_files))
    has_data = any(value is not None for value in (
        diagnosis.setup_slack_ns, diagnosis.hold_slack_ns,
        diagnosis.fmax_mhz, diagnosis.clock_period_ns)) or bool(diagnosis.paths)
    diagnosis.status = "available" if has_data else "unavailable"
    return diagnosis.as_dict()

