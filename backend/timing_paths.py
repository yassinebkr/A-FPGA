"""Immutable timing-path artifact bound to one netlist and constraint set.

Phase G3 normalizes the exact STA run into an ordered, evidence-bound
``timing-paths/v1`` artifact.  The source of truth is OpenSTA's ordered path
table (SiliconCompiler writes ``<top>.topN.rpt`` reports for setup/hold and
each scenario corner).  Every segment keeps its measured delay and cumulative
time; unresolved mappings into the gate netlist are preserved and reported,
never guessed.

The artifact is deliberately independent of SiliconCompiler and FastAPI so
the EDA runner, REST layer, and tests can use it in isolation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from gate_netlist import (GateNetlistError, _canonical_json, _sha256_json,
                          canonical_bytes)

SCHEMA_VERSION = "timing-paths/v1"

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_NUMBER_RE = re.compile(_NUMBER)
_MAX_PATHS = 64
_MAX_SEGMENTS = 128


class TimingPathError(ValueError):
    """Invalid timing-path data, report, or artifact identity."""


def _finite(value: str | None) -> float | None:
    if value is None:
        return None
    match = _NUMBER_RE.search(value)
    if not match:
        return None
    try:
        result = float(match.group(0))
    except ValueError:
        return None
    return result if result == result and abs(result) != float("inf") else None


@dataclass(frozen=True)
class PathSegment:
    """One ordered row of the STA path table."""

    kind: str                    # clock | cell_pin | port | net | summary
    name: str                    # raw STA name (cell/pin, net, port)
    cell_type: str | None = None
    delay_ns: float | None = None
    time_ns: float | None = None
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "cell_type": self.cell_type,
            "delay_ns": self.delay_ns,
            "time_ns": self.time_ns,
            "description": self.description,
        }


@dataclass(frozen=True)
class OrderedPath:
    """One full timing path with its measured arrival/required/slack."""

    analysis: str                # setup | hold
    startpoint: str
    endpoint: str
    clock: str | None = None
    corner: str | None = None
    path_group: str | None = None
    slack_ns: float | None = None
    arrival_ns: float | None = None
    required_ns: float | None = None
    met: bool | None = None
    segments: tuple[PathSegment, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "analysis": self.analysis,
            "startpoint": self.startpoint,
            "endpoint": self.endpoint,
            "clock": self.clock,
            "corner": self.corner,
            "path_group": self.path_group,
            "slack_ns": self.slack_ns,
            "data_arrival_ns": self.arrival_ns,
            "data_required_ns": self.required_ns,
            "met": self.met,
            "segments": [segment.as_dict() for segment in self.segments],
        }


def _analysis_of(path_type: str | None, filename_hint: str = "") -> str:
    text = (path_type or "").lower()
    if "max" in text or "setup" in filename_hint.lower():
        return "setup"
    if "min" in text or "hold" in filename_hint.lower():
        return "hold"
    return "setup" if "setup" in filename_hint.lower() else "hold"


def parse_ordered_report(text: str, *, filename_hint: str = "") -> list[OrderedPath]:
    """Parse OpenSTA's ordered ``report_checks`` style output.

    Every block beginning with ``Startpoint:`` becomes one
    :class:`OrderedPath`.  Table rows are classified by their trailing marker:
    an edge marker (``^``/``v``) identifies pins and ports, ``(net)``
    identifies nets, everything else stays an informational clock row.
    Malformed rows are skipped, never invented; a block without measured
    arrival data is dropped.
    """
    paths: list[OrderedPath] = []
    blocks = re.split(r"(?m)^Startpoint:\s*", text or "")
    for block in blocks[1:]:
        if len(paths) >= _MAX_PATHS:
            break
        header = block.split("\n", 1)
        raw_startpoint = header[0].strip()
        rest = header[1] if len(header) > 1 else ""

        def field_of(label: str) -> str | None:
            match = re.search(rf"(?im)^{label}:\s*(.+)$", rest)
            return match.group(1).strip() if match else None

        # "NAME (description...)" — the name is everything before the first
        # parenthetical qualifier.
        def bare_name(raw: str) -> str:
            return raw.split(" (", 1)[0].strip()

        startpoint = bare_name(raw_startpoint)
        raw_endpoint = field_of("Endpoint")
        endpoint = bare_name(raw_endpoint) if raw_endpoint else None
        path_group = field_of("Path Group")
        path_type = field_of("Path Type")
        corner = field_of("Corner")
        clock = None
        clock_match = re.search(r"clocked by\s+([A-Za-z_][\w$]*)", block)
        if clock_match:
            clock = clock_match.group(1)
        if not startpoint or endpoint is None:
            continue

        analysis = _analysis_of(path_type, filename_hint)
        segments: list[PathSegment] = []
        arrival = None
        required = None
        slack = None
        met = None
        in_required_section = False
        for line in rest.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if set(stripped) <= {"-", " "}:
                continue
            if re.match(r"^(Endpoint|Path Group|Path Type|Corner|Startpoint):", stripped) \
                    or stripped.startswith("Fanout     Cap"):
                continue
            if "data arrival time" in stripped:
                # The arrival section ends here; later occurrences are the
                # required-time summary block, not the measured arrival.
                if arrival is None:
                    match = _NUMBER_RE.search(line)
                    arrival = float(match.group(0)) if match else None
                in_required_section = True
                continue
            if "data required time" in stripped:
                if required is None:
                    match = _NUMBER_RE.search(line)
                    required = float(match.group(0)) if match else None
                continue
            slack_match = re.match(
                rf"\s*({_NUMBER})\s+slack\s+\((MET|VIOLATED|UNCONSTRAINED)", line, re.IGNORECASE)
            if slack_match:
                slack = float(slack_match.group(1))
                met = slack_match.group(2).upper() in ("MET", "UNCONSTRAINED")
                continue
            if in_required_section:
                continue
            # Table row: leading numeric columns, then the description which
            # itself contains digits (cell names), so numbers are only taken
            # from the leading column block.
            columns_match = re.match(rf"\s*((?:{_NUMBER}\s+){{0,4}}{_NUMBER})", line)
            columns = ([float(value) for value in columns_match.group(1).split()]
                       if columns_match else [])
            delay = columns[-2] if len(columns) >= 2 else None
            time = columns[-1] if len(columns) >= 1 else None
            description = line[columns_match.end():].strip() if columns_match else stripped
            pin_match = re.search(r"([v^])\s+(\S+)\s+\(([^)]+)\)\s*$", description)
            net_match = re.search(r"(\S+)\s+\(net\)\s*$", description)
            if pin_match:
                name = pin_match.group(2)
                suffix = pin_match.group(3)
                if suffix in ("out", "in", "inout"):
                    kind = "port"
                    cell_type = None
                else:
                    kind = "cell_pin"
                    cell_type = suffix
            elif net_match:
                name = net_match.group(1)
                kind = "net"
                cell_type = None
            else:
                kind = "clock"
                name = description
                cell_type = None
            if len(segments) >= _MAX_SEGMENTS:
                continue
            segments.append(PathSegment(kind=kind, name=name, cell_type=cell_type,
                                        delay_ns=delay, time_ns=time,
                                        description=description))
        if arrival is None and not segments:
            continue
        paths.append(OrderedPath(
            analysis=analysis, startpoint=startpoint, endpoint=endpoint,
            clock=clock, corner=corner, path_group=path_group,
            slack_ns=slack, arrival_ns=arrival, required_ns=required,
            met=met, segments=tuple(segments)))
    return paths


def _normalize_name(name: str) -> str:
    """Normalize a STA/Yosys escaped identifier for netlist matching.

    Yosys escapes special identifiers with a leading backslash; STA reports
    drop it.  Both spellings must resolve to the same netlist object.
    """
    return name.lstrip("\\").strip()


def map_paths_to_netlist(paths: list[OrderedPath], artifact: Mapping) -> list[dict[str, Any]]:
    """Map every segment to a gate-graph id, preserving unresolved mappings.

    Returns one dict per path with ``segments`` (each carrying ``graph_id``),
    a ``mapping`` block with the resolved/unresolved split, and the honest
    ``partial`` flag the plan requires.
    """
    modules = artifact.get("modules") or {}
    top_module = (artifact.get("identity") or {}).get("top_module")
    top = modules.get(top_module, {})
    cell_names = {cell.get("name"): cell for cell in top.get("cells", [])}
    port_names = {port.get("name"): port for port in top.get("ports", [])}
    net_index: dict[str, str] = {}
    for net in top.get("nets", []):
        for alias in net.get("names") or [net.get("id")]:
            net_index.setdefault(alias, net["id"])
        net_index.setdefault(net["id"], net["id"])

    mapped = []
    for path in paths:
        segments = []
        unresolved: list[str] = []
        resolved = 0
        for segment in path.segments:
            entry = {**segment.as_dict(), "graph_id": None}
            name = _normalize_name(segment.name)
            if segment.kind == "cell_pin" and "/" in name:
                cell_name, pin = name.rsplit("/", 1)
                cell = cell_names.get(cell_name)
                if cell is not None:
                    entry["graph_id"] = f"cell:{cell_name}"
                    entry["pin"] = pin
                    entry["module"] = top_module
                    resolved += 1
                else:
                    unresolved.append(name)
            elif segment.kind == "port":
                port_name = name.split("[")[0]
                if port_name in port_names:
                    entry["graph_id"] = f"port:{port_name}"
                    entry["module"] = top_module
                    resolved += 1
                else:
                    unresolved.append(name)
            elif segment.kind == "net":
                base = name.split("[")[0] if "[" in name else name
                net_id = net_index.get(name) or net_index.get(base) or net_index.get(f"{base}[0]")
                if net_id is not None:
                    entry["graph_id"] = f"net:{top_module}:{net_id}"
                    entry["module"] = top_module
                    resolved += 1
                else:
                    unresolved.append(name)
            elif segment.kind in ("clock", "summary"):
                resolved += 0  # header rows: informational, never "unresolved"
            segments.append(entry)
        mapped.append({
            **path.as_dict(),
            "segments": segments,
            "mapping": {
                "resolved": resolved,
                "unresolved": unresolved,
                "partial": bool(unresolved),
            },
        })
    return mapped


def build_timing_paths(report_files: Mapping[str, str], *, netlist_artifact: Mapping,
                       job_id: str | None = None, project: str | None = None,
                       clock_period_ns: float | None = None) -> dict[str, Any]:
    """Build the canonical timing-paths artifact from the exact STA reports.

    ``report_files`` maps a label (e.g. ``setup.topN``) to the report text.
    The artifact identity binds the netlist artifact digest; a mismatch at
    highlight time is rejected by the consumer, never silently combined.
    """
    if not isinstance(netlist_artifact, Mapping):
        raise TimingPathError("netlist artifact is required")
    identity = netlist_artifact.get("identity") or {}
    paths: list[OrderedPath] = []
    sources: list[str] = []
    for label in sorted(report_files):
        text = report_files[label]
        parsed = parse_ordered_report(text, filename_hint=label)
        if parsed:
            sources.append(label)
            paths.extend(parsed)
    paths = paths[:_MAX_PATHS]
    mapped = map_paths_to_netlist(paths, netlist_artifact)
    artifact = {
        "schema": SCHEMA_VERSION,
        "identity": {
            "job_id": job_id,
            "project": project,
            "netlist_sha256": identity.get("netlist_sha256"),
            "gate_artifact_sha256": netlist_artifact.get("artifact_sha256"),
            "source_sha256": identity.get("source_sha256"),
            "constraints_sha256": identity.get("constraints_sha256"),
            "clock_period_ns": clock_period_ns,
            "tool": "opensta",
            "top_module": identity.get("top_module"),
            "sources": sources,
            "path_count": len(mapped),
            "complete": all(not path["mapping"]["partial"] for path in mapped) if mapped else False,
        },
        "paths": mapped,
    }
    artifact["artifact_sha256"] = _sha256_json(artifact)
    return artifact


def load_timing_paths(data: Any) -> dict[str, Any]:
    """Validate a stored timing-path artifact, verifying its own digest."""
    if not isinstance(data, Mapping):
        raise TimingPathError("timing path artifact must be an object")
    if data.get("schema") != SCHEMA_VERSION:
        raise TimingPathError(f"unsupported timing path schema {data.get('schema')!r}")
    artifact = dict(data)
    declared = artifact.get("artifact_sha256")
    if not isinstance(declared, str) or len(declared) != 64:
        raise TimingPathError("timing path artifact digest is invalid")
    body = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
    if _sha256_json(body) != declared:
        raise TimingPathError("timing path artifact digest does not match its content")
    if not isinstance(artifact.get("paths"), list):
        raise TimingPathError("timing path artifact paths must be a list")
    return artifact


def timing_paths_unavailable(reason: str) -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "available": False, "reason": reason}


# Ordered reports SiliconCompiler's opensta step writes for a completed run.
_REPORT_GLOBS = (
    "timing/0/reports/timing/setup.topN.rpt",
    "timing/0/reports/timing/hold.topN.rpt",
)


def collect_timing_paths(build_dir: str | os.PathLike, top_module: str,
                         netlist_artifact: Mapping, *,
                         job_id: str | None = None,
                         project: str | None = None,
                         clock_period_ns: float | None = None) -> dict[str, Any]:
    """Collect, build, and validate the timing-path artifact for one job.

    Returns the manifest metadata (never the artifact itself) or an honest
    ``unavailable`` record when the STA run produced no ordered reports.
    """
    job0 = Path(build_dir) / top_module / "job0"
    reports: dict[str, str] = {}
    for pattern in _REPORT_GLOBS:
        matches = sorted(job0.glob(pattern))
        if not matches:
            continue
        label = pattern.rsplit("/", 1)[-1].replace(".rpt", "")
        try:
            reports[label] = matches[-1].read_text(encoding="utf-8", errors="replace")[:2_000_000]
        except OSError:
            continue
    # Scenario corners carry their own labels so setup/hold/corner stay
    # distinguishable in the UI.
    for corner_dir in sorted((job0 / "timing/0/reports/timing/scenarios").glob("*")):
        for report in sorted(corner_dir.glob("*.topN.rpt")):
            label = f"{report.stem}.{corner_dir.name}"
            try:
                reports[label] = report.read_text(encoding="utf-8", errors="replace")[:2_000_000]
            except OSError:
                continue
    if not reports:
        return timing_paths_unavailable(
            "the STA run produced no ordered timing-path reports")
    try:
        artifact = build_timing_paths(
            reports, netlist_artifact=netlist_artifact, job_id=job_id,
            project=project, clock_period_ns=clock_period_ns)
        artifact = load_timing_paths(artifact)
    except (TimingPathError, GateNetlistError, ValueError, TypeError) as exc:
        return timing_paths_unavailable(f"timing-path reports rejected: {exc}")
    content = canonical_bytes(artifact)
    summary = dict(artifact["identity"])
    summary.update({
        "available": True,
        "artifact_sha256": artifact["artifact_sha256"],
        "repository_sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    })
    summary["artifact"] = artifact
    return summary
