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
import re
from pathlib import Path
from typing import Any, Mapping

from gate_netlist import GateNetlistError, canonical_bytes, _sha256_json, \
    _truthy_attribute  # noqa: F401  (_truthy_attribute re-exported for parity)

SCHEMA_VERSION = "layout-view/v1"

# A routed DEF of a large core can carry hundreds of thousands of components;
# the indexed view is bounded so the browser never receives an unbounded
# geometry document by default.
MAX_COMPONENTS = 20000
MAX_PINS = 512
MAX_DEF_BYTES = 256 * 1024 * 1024

_NUMBER = r"-?\d+"


class LayoutError(ValueError):
    """Invalid DEF data, artifact identity, or layout request."""


def _normalize_name(name: str) -> str:
    r"""Unescape a DEF identifier (q\[0\]$FF -> q[0]$FF) and drop the Yosys
    leading-backslash spelling so netlist names resolve either way."""
    return name.strip().replace("\\", "").strip()


def parse_def(text: str) -> dict[str, Any]:
    """Parse the bounded subset of LEF/DEF needed for the layout view.

    Extracts design name, units, DIEAREA, ROW statements, COMPONENTS with
    placement, and PINS with placement.  Malformed statements are skipped,
    never invented.
    """
    if not isinstance(text, str) or not text.strip():
        raise LayoutError("DEF document is empty")

    units_match = re.search(r"(?im)^UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;", text)
    units = int(units_match.group(1)) if units_match else 1000
    design_match = re.search(r"(?im)^DESIGN\s+(\S+)\s*;", text)
    design = design_match.group(1) if design_match else None

    die = None
    die_match = re.search(r"(?im)^DIEAREA\s+((?:\(\s*-?\d+\s+-?\d+\s*\)\s*)+);", text)
    if die_match:
        coordinates = [int(value) for value in re.findall(r"-?\d+", die_match.group(1))]
        if len(coordinates) >= 4:
            xs = coordinates[0::2]
            ys = coordinates[1::2]
            die = {"llx": min(xs), "lly": min(ys),
                   "urx": max(xs), "ury": max(ys)}

    rows = []
    for match in re.finditer(
            r"(?im)^ROW\s+(\S+)\s+(\S+)\s+(-?\d+)\s+(-?\d+)\s+(\S+)\s+"
            r"DO\s+(\d+)\s+BY\s+(\d+)\s+STEP\s+(\d+)\s+(\d+)\s*;", text):
        rows.append({
            "name": match.group(1), "site": match.group(2),
            "x": int(match.group(3)), "y": int(match.group(4)),
            "orientation": match.group(5),
            "num_x": int(match.group(6)), "num_y": int(match.group(7)),
            "step_x": int(match.group(8)), "step_y": int(match.group(9)),
        })

    components = []
    unplaced = 0
    truncated = False
    section = re.search(r"(?is)COMPONENTS\s+\d+\s*;(.*?)END\s+COMPONENTS", text)
    if section:
        for statement in re.finditer(
                r"-\s+(\S+)\s+(\S+)(.*?);", section.group(1), re.DOTALL):
            if len(components) >= MAX_COMPONENTS:
                truncated = True
                break
            name = _normalize_name(statement.group(1))
            macro = statement.group(2)
            rest = statement.group(3)
            placed = re.search(r"(FIXED|PLACED)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+(\S+)", rest)
            if not name or not macro:
                continue
            if placed:
                components.append({
                    "name": name, "macro": macro,
                    "x": int(placed.group(2)), "y": int(placed.group(3)),
                    "orientation": placed.group(4),
                })
            else:
                unplaced += 1

    pins = []
    pin_section = re.search(r"(?is)PINS\s+\d+\s*;(.*?)END\s+PINS", text)
    if pin_section:
        for statement in re.finditer(
                r"-\s+(\S+)(.*?);", pin_section.group(1), re.DOTALL):
            if len(pins) >= MAX_PINS:
                break
            body = statement.group(2)
            name = _normalize_name(statement.group(1))
            if not name:
                continue
            # Direction appears as `+ DIRECTION <dir>` (OpenROUTE DEF) or in
            # the legacy `( * INPUT )` spelling.
            direction_match = (re.search(r"\+\s*DIRECTION\s+(INPUT|OUTPUT|INOUT|FEEDTHRU)", body, re.I)
                              or re.search(r"\(\s*\*\s+(INPUT|OUTPUT|INOUT|FEEDTHRU)\s*\)", body, re.I))
            net_match = re.search(r"\+\s*NET\s+(\S+)", body)
            placed = re.search(r"(?:PLACED|FIXED)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)", body)
            pins.append({
                "name": name,
                "direction": direction_match.group(1).upper() if direction_match else None,
                "net": _normalize_name(net_match.group(1)) if net_match else None,
                "x": int(placed.group(1)) if placed else None,
                "y": int(placed.group(2)) if placed else None,
            })

    if die is None and not components:
        raise LayoutError("DEF carries neither a DIEAREA nor placed components")

    return {
        "design": design, "units": units, "die": die, "rows": rows,
        "components": components, "unplaced_count": unplaced,
        "pins": pins, "components_truncated": truncated,
    }


def _nominal_footprint(rows: list[dict[str, Any]]) -> dict[str, int] | None:
    """The DEF's own rows imply a nominal std-cell footprint.

    A DEF row's STEP carries the site width; the row pitch (difference
    between consecutive row origins) carries the site height. Exact LEF
    macro dimensions are deliberately not guessed: the view flags
    ``nominal_cell_footprint`` so the UI can say the rectangles are nominal.
    """
    width = next((row["step_x"] for row in rows if row["step_x"] > 0), None)
    height = next((row["step_y"] for row in rows if row["step_y"] > 0), None)
    if height is None and len(rows) >= 2:
        pitches = sorted({row["y"] for row in rows})
        deltas = [b - a for a, b in zip(pitches, pitches[1:]) if b > a]
        height = min(deltas) if deltas else None
    if width and height:
        return {"w": width, "h": height}
    return None


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
    if isinstance(netlist_artifact, Mapping):
        netlist_identity = netlist_artifact.get("identity") or {}
        identity["netlist_sha256"] = netlist_identity.get("netlist_sha256")
        identity["gate_artifact_sha256"] = netlist_artifact.get("artifact_sha256")
        identity["source_sha256"] = netlist_identity.get("source_sha256")
        identity["constraints_sha256"] = netlist_identity.get("constraints_sha256")
        top_module = netlist_identity.get("top_module")
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
    artifact["artifact_sha256"] = _sha256_json(artifact)
    return artifact


def load_layout_view(data: Any) -> dict[str, Any]:
    """Validate a stored layout-view artifact, verifying its own digest."""
    if not isinstance(data, Mapping):
        raise LayoutError("layout view artifact must be an object")
    if data.get("schema") != SCHEMA_VERSION:
        raise LayoutError(f"unsupported layout view schema {data.get('schema')!r}")
    artifact = dict(data)
    declared = artifact.get("artifact_sha256")
    if not isinstance(declared, str) or len(declared) != 64:
        raise LayoutError("layout view artifact digest is invalid")
    body = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
    if _sha256_json(body) != declared:
        raise LayoutError("layout view artifact digest does not match its content")
    if not isinstance(artifact.get("components"), list):
        raise LayoutError("layout view components must be a list")
    for component in artifact["components"]:
        if not isinstance(component, Mapping) or not isinstance(component.get("name"), str):
            raise LayoutError("layout view components must carry names")
    return artifact


def layout_view_unavailable(reason: str) -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "available": False, "reason": reason}


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
                def_text = handle.read(MAX_DEF_BYTES + 1).decode("utf-8", errors="replace")
            if len(def_text) > MAX_DEF_BYTES:
                return layout_view_unavailable("DEF exceeds the size bound")
        else:
            def_text = def_path.read_text(encoding="utf-8", errors="replace")
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
