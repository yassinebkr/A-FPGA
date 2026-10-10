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
import os
from pathlib import Path
from typing import Any, Mapping

from gate_netlist import GateNetlistError
from visualization_artifacts import (canonical_bytes, load_sealed,
                                      netlist_identity, seal, unavailable)

SCHEMA_VERSION = "timing-paths/v1"

from timing_path_parser import (
    OrderedPath, PathSegment, TimingPathError, _MAX_PATHS, _MAX_REPORTS,
    _MAX_REPORT_BYTES, _MAX_SEGMENTS, _analysis_of, _finite,
    _normalize_name, map_paths_to_netlist, parse_ordered_report,
)

def build_timing_paths(report_files: Mapping[str, str], *, netlist_artifact: Mapping,
                       job_id: str | None = None, project: str | None = None,
                       clock_period_ns: float | None = None) -> dict[str, Any]:
    """Build the canonical timing-paths artifact from the exact STA reports.

    ``report_files`` maps a label (e.g. ``setup.topN``) to the report text.
    The artifact identity binds the netlist artifact digest; a mismatch at
    highlight time is rejected by the consumer, never silently combined.
    """
    if not isinstance(report_files, Mapping):
        raise TimingPathError("timing reports must be an object")
    if len(report_files) > _MAX_REPORTS:
        raise TimingPathError(f"too many timing reports (maximum {_MAX_REPORTS})")
    if not isinstance(netlist_artifact, Mapping):
        raise TimingPathError("netlist artifact is required")
    identity = netlist_identity(netlist_artifact)
    paths: list[OrderedPath] = []
    sources: list[str] = []
    for label in sorted(report_files):
        if not isinstance(label, str) or not label or len(label) > 256:
            raise TimingPathError("timing report labels must be short strings")
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
            "gate_artifact_sha256": identity.get("gate_artifact_sha256"),
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
    return seal(artifact)


def load_timing_paths(data: Any) -> dict[str, Any]:
    """Validate a stored timing-path artifact, verifying its own digest."""
    artifact = load_sealed(data, schema=SCHEMA_VERSION,
                           label="timing path artifact",
                           error_type=TimingPathError)
    if not isinstance(artifact.get("paths"), list):
        raise TimingPathError("timing path artifact paths must be a list")
    if len(artifact["paths"]) > _MAX_PATHS:
        raise TimingPathError(f"timing path artifact exceeds {_MAX_PATHS} paths")
    for path in artifact["paths"]:
        if not isinstance(path, Mapping):
            raise TimingPathError("timing path entries must be objects")
        segments = path.get("segments")
        if not isinstance(segments, list) or len(segments) > _MAX_SEGMENTS:
            raise TimingPathError("timing path segments exceed the size bound")
        mapping = path.get("mapping")
        if not isinstance(mapping, Mapping):
            raise TimingPathError("timing path mapping must be an object")
        unresolved = mapping.get("unresolved", [])
        if not isinstance(unresolved, list) or len(unresolved) > _MAX_SEGMENTS:
            raise TimingPathError("timing path unresolved mappings exceed the size bound")
        for segment in segments:
            if not isinstance(segment, Mapping):
                raise TimingPathError("timing path segments must be objects")
    return artifact


def timing_paths_unavailable(reason: str) -> dict[str, Any]:
    return unavailable(SCHEMA_VERSION, reason)


# Ordered reports SiliconCompiler's OpenSTA steps write for a completed run.
# ``timing`` is the historical screening step.  Full flows use named stages;
# the discovery helper below selects the deepest available stage without
# rerunning or changing the build.
_REPORT_NAMES = ("setup.topN.rpt", "hold.topN.rpt")


def _timing_report_root(job0: Path) -> Path | None:
    """Find the latest available ordered-report directory in one job."""
    candidates = [path for path in job0.glob("*/0/reports/timing")
                  if path.is_dir() and any((path / name).is_file()
                                           for name in _REPORT_NAMES)]
    if not candidates:
        return None
    # SiliconCompiler's stage names are stable but lexical ordering places
    # ``synthesis`` after some physical steps.  Rank known stages explicitly,
    # then use the path as a deterministic tie-breaker for custom flows.
    stage_rank = {
        "timing": 10,
        "synthesis.timing": 20,
        "floorplan.macro_placement": 30,
        "floorplan.pin_placement": 35,
        "floorplan.tapcell": 40,
        "floorplan.power_grid": 45,
        "place.global": 50,
        "place.detailed": 55,
        "cts.clock_tree_synthesis": 60,
        "cts.repair_timing": 65,
        "route.global": 70,
        "route.detailed": 75,
        "route.detailed_antenna_repair": 80,
        "route.antenna_repair": 85,
        "write.views": 90,
    }
    return max(candidates, key=lambda path: (
        stage_rank.get(path.parts[-4], 1), str(path)))


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
    report_root = _timing_report_root(job0)
    if report_root is None:
        return timing_paths_unavailable(
            "the STA run produced no ordered timing-path reports")
    for report_path in (report_root / name for name in _REPORT_NAMES):
        if not report_path.is_file():
            continue
        label = report_path.name.replace(".rpt", "")
        try:
            reports[label] = report_path.read_text(
                encoding="utf-8", errors="replace")[:_MAX_REPORT_BYTES]
        except OSError:
            continue
    # Scenario corners carry their own labels so setup/hold/corner stay
    # distinguishable in the UI.
    for corner_dir in sorted((report_root / "scenarios").glob("*")):
        for report in sorted(corner_dir.glob("*.topN.rpt")):
            label = f"{report.stem}.{corner_dir.name}"
            try:
                reports[label] = report.read_text(encoding="utf-8", errors="replace")[:_MAX_REPORT_BYTES]
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
