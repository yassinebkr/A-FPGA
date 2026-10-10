"""Read-only qualification report for an existing SiliconCompiler build.

This CLI consumes evidence already present below a SiliconCompiler build root.
It calls the production gate-netlist, timing-path, and physical-layout
collectors and emits metadata only; no artifact is persisted and no EDA or
model process is launched.

Example::

    .venv/bin/python qualify_existing_build.py \\
      /mnt/cold-hdd/sc_builds/phase2-ibex-specialized-pnr-filetype-fix ibex_core
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

import gate_netlist
import physical_layout
import timing_paths


SCHEMA_VERSION = "build-qualification/v1"


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def find_netlist(build_root: Path, top_module: str) -> Path | None:
    """Select the most authoritative structured netlist below one build."""
    candidates = sorted(
        path for path in build_root.rglob(f"{top_module}.netlist.json")
        if path.is_file() and not path.is_symlink() and _inside(build_root, path))
    if not candidates:
        return None
    preferred = [path for path in candidates
                 if path.parent.name == "outputs"
                 and path.parent.parent.name == "0"
                 and path.parent.parent.parent.name == "synthesis"]
    return preferred[-1] if preferred else candidates[-1]


def _timed(callable_, *args, **kwargs) -> tuple[Any, float]:
    started = time.perf_counter()
    result = callable_(*args, **kwargs)
    return result, (time.perf_counter() - started) * 1000


def _without_artifact(summary: Any) -> dict[str, Any]:
    """Keep the report compact while proving the collector validated output."""
    if not isinstance(summary, dict):
        return {"status": "error", "reason": "collector returned a non-object"}
    result = {key: value for key, value in summary.items() if key != "artifact"}
    result["status"] = "available" if summary.get("available") else "unavailable"
    if summary.get("available") and isinstance(summary.get("artifact"), dict):
        artifact = summary["artifact"]
        result["validated"] = True
        result["artifact_schema"] = artifact.get("schema")
        result["artifact_sha256"] = artifact.get("artifact_sha256")
    else:
        result["validated"] = False
    return result


def qualify_existing_build(
    build_root: str | os.PathLike[str],
    top_module: str,
    *,
    job_id: str | None = None,
    project: str | None = None,
    clock_period_ns: float | None = None,
) -> dict[str, Any]:
    """Collect and validate existing build evidence without changing it."""
    root = Path(build_root).expanduser()
    if not root.is_dir():
        raise ValueError(f"build root does not exist or is not a directory: {root}")
    if not isinstance(top_module, str) or not top_module:
        raise ValueError("top_module must be a non-empty string")

    netlist_path = find_netlist(root, top_module)
    if netlist_path is None:
        netlist = gate_netlist.unavailable(
            "no structured Yosys netlist JSON was found below the build root")
        netlist_elapsed = 0.0
    else:
        netlist, netlist_elapsed = _timed(
            gate_netlist.build_job_netlist_manifest,
            str(netlist_path), top_module=top_module, job_id=job_id,
            project=project)

    netlist_artifact = netlist.get("artifact") if isinstance(netlist, dict) else None
    if isinstance(netlist_artifact, dict):
        source_hash = netlist_artifact.get("identity", {}).get("source_sha256")
    else:
        source_hash = None

    if isinstance(netlist_artifact, dict) and netlist.get("available"):
        timing, timing_elapsed = _timed(
            timing_paths.collect_timing_paths,
            root, top_module, netlist_artifact, job_id=job_id,
            project=project, clock_period_ns=clock_period_ns)
    else:
        timing = timing_paths.timing_paths_unavailable(
            "timing paths require a validated gate-netlist artifact")
        timing_elapsed = 0.0
    layout, layout_elapsed = _timed(
        physical_layout.collect_layout_view,
        root, top_module, netlist_artifact, job_id=job_id,
        project=project, clock_period_ns=clock_period_ns)

    report = {
        "schema": SCHEMA_VERSION,
        "read_only": True,
        "eda_invoked": False,
        "model_invoked": False,
        "build_root": str(root.resolve()),
        "top_module": top_module,
        "job_id": job_id,
        "project": project,
        "source_sha256": source_hash,
        "netlist": {
            **_without_artifact(netlist),
            "path": str(netlist_path.relative_to(root)) if netlist_path else None,
            "elapsed_ms": round(netlist_elapsed, 3),
        },
        "timing": {
            **_without_artifact(timing),
            "elapsed_ms": round(timing_elapsed, 3),
            "report_root": _relative_timing_root(root, top_module),
        },
        "layout": {
            **_without_artifact(layout),
            "elapsed_ms": round(layout_elapsed, 3),
            "def_path": _relative_def_path(root, top_module),
        },
    }
    available = [report[name]["status"] == "available"
                 for name in ("netlist", "timing", "layout")]
    quality_flags: list[str] = []
    netlist_completeness = report["netlist"].get("completeness") or {}
    if netlist_completeness.get("complete") is False:
        quality_flags.append("netlist completeness is partial")
    if report["timing"].get("complete") is False:
        quality_flags.append("timing-path mapping is partial")
    layout_completeness = report["layout"].get("completeness") or {}
    if layout_completeness.get("truncated"):
        quality_flags.append("layout component collection is truncated")
    if layout_completeness.get("mapping_partial"):
        quality_flags.append("layout-to-netlist mapping is partial")
    report["qualification"] = {
        "status": "passed" if all(available) and not quality_flags else "partial",
        "available": {
            "netlist": available[0],
            "timing": available[1],
            "layout": available[2],
        },
        "quality_flags": quality_flags,
        "limitations": [
            "qualification reads existing evidence and performs no EDA run",
            "artifact persistence and browser interaction are outside this CLI",
        ],
    }
    return report


def _relative_timing_root(root: Path, top_module: str) -> str | None:
    job0 = root / top_module / "job0"
    finder = getattr(timing_paths, "_timing_report_root", None)
    path = finder(job0) if callable(finder) else None
    return str(path.relative_to(root)) if path is not None else None


def _relative_def_path(root: Path, top_module: str) -> str | None:
    path = physical_layout.find_final_def(root, top_module)
    return str(path.relative_to(root)) if path is not None else None


def _main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build_root", type=Path)
    parser.add_argument("top_module")
    parser.add_argument("--job-id")
    parser.add_argument("--project")
    parser.add_argument("--clock-period-ns", type=float)
    args = parser.parse_args(argv)
    report = qualify_existing_build(
        args.build_root, args.top_module, job_id=args.job_id,
        project=args.project, clock_period_ns=args.clock_period_ns)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a CLI
    raise SystemExit(_main())
