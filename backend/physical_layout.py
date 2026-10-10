"""Indexed, browser-safe physical layout view from the exact post-P&R DEF.

Phase G4 derives an interactive layout representation from the routed DEF of
one job — never from GDS (the binary layout stays downloadable evidence).
The view is bounded and indexed: die/core outline, rows, placed components
with their netlist-mapped graph ids, and IO pins.  Cell footprints are
nominal (DEF placement carries coordinates, not LEF dimensions) and are
flagged as such; nothing about the geometry is invented beyond the nominal
footprint that the DEF's own row site implies.

The artifact is ``layout-view/v1``, digest-bound to the same netlist,
constraint, and job identity as the gate netlist and timing paths, and is
persisted through the existing evidence store.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

from gate_netlist import GateNetlistError  # noqa: F401 (public compatibility)
from visualization_artifacts import (canonical_bytes, load_sealed,
                                      netlist_identity, seal, unavailable)

SCHEMA_VERSION = "layout-view/v1"

from physical_def_parser import (MAX_COMPONENTS, MAX_DEF_BYTES, MAX_PINS,
                                  LayoutError, _nominal_footprint,
                                  _normalize_name, parse_def)


def build_layout_view(def_text: str, *, netlist_artifact: Mapping | None = None,
                      job_id: str | None = None, project: str | None = None,
                      clock_period_ns: float | None = None,
                      violations: Mapping | None = None) -> dict[str, Any]:
    """Build the canonical layout-view artifact from the exact DEF text."""
    parsed = parse_def(def_text)
    identity: dict[str, Any] = {
        "job_id": job_id,
        "project": project,
        "clock_period_ns": clock_period_ns,
        "def_sha256": hashlib.sha256(def_text.encode("utf-8")).hexdigest(),
        "design": parsed["design"],
    }
    # Map DEF component names onto the netlist's gate-graph cell ids so a
    # schematic selection locates the same instance in the physical view.
    top_module = None
    cell_names: set[str] = set()
    shared_identity = netlist_identity(netlist_artifact)
    if isinstance(netlist_artifact, Mapping):
        identity.update({key: shared_identity.get(key) for key in (
            "netlist_sha256", "gate_artifact_sha256", "source_sha256",
            "constraints_sha256")})
        top_module = shared_identity.get("top_module")
        modules = netlist_artifact.get("modules") or {}
        for name, module in modules.items():
            for cell in (module.get("cells") or []):
                if isinstance(cell.get("name"), str):
                    cell_names.add(_normalize_name(cell["name"]))

    components = []
    mapped = 0
    for component in parsed["components"]:
        graph_id = f"cell:{component['name']}" if component["name"] in cell_names else None
        if graph_id:
            mapped += 1
        entry = dict(component)
        entry["graph_id"] = graph_id
        components.append(entry)

    footprint = _nominal_footprint(parsed["rows"])
    artifact: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "identity": identity,
        "units": parsed["units"],
        "die": parsed["die"],
        "rows": parsed["rows"],
        "components": components,
        "pins": parsed["pins"],
        "nominal_footprint": footprint,
        "completeness": {
            "component_count": len(components),
            "unplaced_count": parsed["unplaced_count"],
            "truncated": bool(parsed["components_truncated"]),
            "mapped_to_netlist": mapped,
            "mapping_partial": isinstance(netlist_artifact, Mapping)
            and mapped < len(components),
            "nominal_cell_footprint": footprint is not None,
            "pin_count": len(parsed["pins"]),
            "violations_reported": bool(violations),
        },
    }
    if isinstance(violations, Mapping):
        artifact["violations"] = violations
    return seal(artifact)


def load_layout_view(data: Any) -> dict[str, Any]:
    """Validate a stored layout-view artifact, verifying its own digest."""
    artifact = load_sealed(data, schema=SCHEMA_VERSION,
                           label="layout view artifact",
                           error_type=LayoutError)
    if not isinstance(artifact.get("components"), list):
        raise LayoutError("layout view components must be a list")
    if len(artifact["components"]) > MAX_COMPONENTS:
        raise LayoutError(f"layout view exceeds {MAX_COMPONENTS} components")
    names: set[str] = set()
    for component in artifact["components"]:
        if not isinstance(component, Mapping) or not isinstance(component.get("name"), str):
            raise LayoutError("layout view components must carry names")
        if component["name"] in names:
            raise LayoutError(f"layout view contains duplicate component {component['name']!r}")
        names.add(component["name"])
    pins = artifact.get("pins", [])
    if not isinstance(pins, list) or len(pins) > MAX_PINS:
        raise LayoutError(f"layout view pins exceed {MAX_PINS}")
    pin_names: set[str] = set()
    for pin in pins:
        if not isinstance(pin, Mapping) or not isinstance(pin.get("name"), str):
            raise LayoutError("layout view pins must carry names")
        if pin["name"] in pin_names:
            raise LayoutError(f"layout view contains duplicate pin {pin['name']!r}")
        pin_names.add(pin["name"])
    return artifact


def layout_view_unavailable(reason: str) -> dict[str, Any]:
    return unavailable(SCHEMA_VERSION, reason)


def find_final_def(build_dir: str | os.PathLike, top_module: str) -> Path | None:
    """Deepest DEF emitted by the flow (floorplan -> route).

    SiliconCompiler stores routed DEFs gzipped (``<top>.def.gz``); both
    spellings are flow outputs.
    """
    job0 = Path(build_dir) / top_module / "job0"
    if not job0.is_dir():
        return None
    candidates = []
    try:
        steps = sorted(job0.glob("*/0/outputs"))
    except OSError:
        return None
    for outputs in steps:
        for name in (f"{top_module}.def", f"{top_module}.def.gz"):
            candidate = outputs / name
            if candidate.is_file():
                candidates.append(candidate)
    return candidates[-1] if candidates else None


def collect_layout_view(build_dir: str | os.PathLike, top_module: str,
                        netlist_artifact: Mapping | None = None, *,
                        job_id: str | None = None,
                        project: str | None = None,
                        clock_period_ns: float | None = None,
                        violations: Mapping | None = None) -> dict[str, Any]:
    """Collect, build, and validate the layout view for one job.

    Returns the manifest metadata (never the artifact itself) or an honest
    ``unavailable`` record when the flow emitted no DEF.
    """
    try:
        def_path = find_final_def(build_dir, top_module)
        if def_path is None:
            return layout_view_unavailable(
                "the flow emitted no DEF: no physical implementation exists for this job")
        if def_path.stat().st_size > MAX_DEF_BYTES:
            return layout_view_unavailable("DEF exceeds the size bound")
        if def_path.suffix == ".gz":
            import gzip
            with gzip.open(def_path, "rb") as handle:
                raw_def = handle.read(MAX_DEF_BYTES + 1)
        else:
            with def_path.open("rb") as handle:
                raw_def = handle.read(MAX_DEF_BYTES + 1)
        if len(raw_def) > MAX_DEF_BYTES:
            return layout_view_unavailable("DEF exceeds the size bound")
        def_text = raw_def.decode("utf-8", errors="replace")
        artifact = build_layout_view(
            def_text, netlist_artifact=netlist_artifact, job_id=job_id,
            project=project, clock_period_ns=clock_period_ns,
            violations=violations)
        artifact = load_layout_view(artifact)
    except (LayoutError, GateNetlistError, OSError, ValueError, TypeError) as exc:
        return layout_view_unavailable(f"DEF rejected: {exc}")
    content = canonical_bytes(artifact)
    identity = dict(artifact["identity"])
    identity.update({
        "available": True,
        "artifact_sha256": artifact["artifact_sha256"],
        "repository_sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "completeness": artifact["completeness"],
    })
    identity["artifact"] = artifact
    return identity
