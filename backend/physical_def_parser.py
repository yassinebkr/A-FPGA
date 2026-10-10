"""Bounded DEF parsing for the physical-layout artifact.

This module has no persistence or API concerns. It turns a routed DEF text
document into bounded, validated placement records for the layout builder.
"""

from __future__ import annotations

import re
from typing import Any

MAX_COMPONENTS = 20000
MAX_PINS = 512
MAX_DEF_BYTES = 256 * 1024 * 1024

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
    if len(text.encode("utf-8")) > MAX_DEF_BYTES:
        raise LayoutError("DEF exceeds the size bound")

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
    component_names: set[str] = set()
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
            if name in component_names:
                raise LayoutError(f"duplicate DEF component {name!r}")
            component_names.add(name)
            if placed:
                components.append({
                    "name": name, "macro": macro,
                    "x": int(placed.group(2)), "y": int(placed.group(3)),
                    "orientation": placed.group(4),
                })
            else:
                unplaced += 1

    pins = []
    pin_names: set[str] = set()
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
            if name in pin_names:
                raise LayoutError(f"duplicate DEF pin {name!r}")
            pin_names.add(name)
            # Direction appears as a DIRECTION clause (OpenROUTE DEF) or in
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
