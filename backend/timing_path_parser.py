"""Bounded OpenSTA report parsing and netlist path mapping.

The timing artifact builder owns identity, sealing, persistence, and API
metadata. This module owns the pure parser and bounded mapping operations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_NUMBER_RE = re.compile(_NUMBER)
_MAX_PATHS = 64
_MAX_SEGMENTS = 128
_MAX_REPORTS = 32
_MAX_REPORT_BYTES = 2_000_000


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
    if not isinstance(text, str):
        raise TimingPathError("timing report must be text")
    # The collector applies the same bound while reading files. Applying it
    # here protects the independently usable parser from oversized input and
    # keeps regex work bounded.
    if len(text.encode("utf-8")) > _MAX_REPORT_BYTES:
        raise TimingPathError("timing report exceeds the size bound")
    paths: list[OrderedPath] = []
    blocks = re.split(r"(?m)^Startpoint:\s*", text)
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
