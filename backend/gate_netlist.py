"""Canonical synthesized gate-netlist artifact and bounded graph views.

A successful synthesis job is the only legitimate source of a gate-level
diagram.  This module turns the synthesis tool's structured netlist output
(Yosys ``write_json``) into an immutable, content-addressed artifact
(``gate-netlist/v1``) and serves bounded hierarchical views of it
(``gate-graph/v1``).  Nothing here invents gates, hierarchy, or nets: if the
structured output is absent the artifact is reported ``unavailable``, and a
diagram is never fabricated from the RTL declaration index.

Design rules (workbench-density-and-gate-visualization-plan.md, Phase G1):

* The artifact carries schema version, its own SHA-256, and the exact
  project/source/constraint/job/netlist digests it was built from.
* Every graph request is bounded by module scope, hierarchy depth, and a node
  limit; the browser never receives an unbounded flattened design.
* Stale or mismatched digests are rejected, never silently combined.
* Completeness is explicit: truncation, unsupported primitives, unknown pin
  directions, and black-box modules are reported, not guessed.

The module has no SiliconCompiler, asyncio, or FastAPI dependency so the EDA
runner, the REST layer, and tests can use it independently.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Mapping

SCHEMA_VERSION = "gate-netlist/v1"
GRAPH_SCHEMA_VERSION = "gate-graph/v1"

# Request bounds. A request outside these ranges is invalid, not truncated:
# the caller must ask for a bounded view explicitly.
MAX_NODE_LIMIT = 2000
MAX_DEPTH = 4
MAX_CONE_HOPS = 16
# Refuse to canonicalize pathological outputs rather than exhausting memory.
MAX_YOSYS_JSON_BYTES = 256 * 1024 * 1024

_BIT_CONSTANTS = frozenset({"0", "1", "x", "z"})
_PORT_DIRECTIONS = frozenset({"input", "output", "inout"})
_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GateNetlistError(ValueError):
    """Invalid netlist data, artifact identity, or graph request."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def canonical_bytes(artifact: Mapping) -> bytes:
    """Deterministic byte form used for persistence and digests."""
    return _canonical_json(artifact).encode("utf-8")


def _require_digest(value: Any, label: str, *, optional: bool = False) -> str | None:
    if value is None:
        if optional:
            return None
        raise GateNetlistError(f"{label} digest is required")
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise GateNetlistError(f"{label} must be a SHA-256 digest")
    return value


def _validate_bits(bits: Any, label: str) -> list[Any]:
    if not isinstance(bits, list):
        raise GateNetlistError(f"{label} bits must be a list")
    for bit in bits:
        if isinstance(bit, bool):
            raise GateNetlistError(f"{label} contains a boolean bit")
        if isinstance(bit, int):
            if bit < 0:
                raise GateNetlistError(f"{label} contains a negative net bit")
        elif isinstance(bit, str):
            if bit not in _BIT_CONSTANTS:
                raise GateNetlistError(
                    f"{label} contains an invalid constant bit {bit!r}")
        else:
            raise GateNetlistError(f"{label} contains an invalid bit {bit!r}")
    return bits


def _plain_value(value: Any) -> Any:
    """Keep only JSON-primitive parameter values, deterministically."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _plain_value(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_plain_value(v) for v in value]
    return str(value)


def _clean_source(value: Any) -> str | None:
    """Keep a tool-supplied source attribute, bounded and string-typed."""
    if isinstance(value, str) and 0 < len(value) < 512:
        return value
    return None


def _truthy_attribute(value: Any) -> bool:
    """Decode a Yosys attribute value.

    Yosys ``write_json`` emits integer attributes as ASCII bit-vectors (for
    example ``"00000000000000000000000000000001"`` for 1), so a plain
    ``in (1, "1", True)`` check misses every real ``blackbox`` flag emitted
    by ``read_liberty -lib``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        try:
            return int(value, 2) != 0
        except ValueError:
            return bool(value.strip())
    return False


def build_gate_netlist(yosys_data: Any, *, top_module: str,
                       job_id: str | None = None,
                       project: str | None = None,
                       source_sha256: str | None = None,
                       constraints_sha256: str | None = None,
                       netlist_sha256: str | None = None,
                       tool: str = "yosys",
                       tool_version: str | None = None,
                       library: str | None = None) -> dict[str, Any]:
    """Normalize a Yosys ``write_json`` document into the canonical artifact.

    ``netlist_sha256`` should be the digest of the exact JSON document the
    synthesis subprocess produced; when omitted it is computed here so the
    artifact still binds the exact netlist content.
    """
    if not isinstance(yosys_data, Mapping):
        raise GateNetlistError("Yosys JSON must be an object")
    modules_raw = yosys_data.get("modules")
    if not isinstance(modules_raw, Mapping):
        raise GateNetlistError("Yosys JSON must contain a 'modules' object")
    if not isinstance(top_module, str) or not _MODULE_NAME.fullmatch(top_module):
        raise GateNetlistError("invalid top module name")
    if top_module not in modules_raw:
        raise GateNetlistError(f"top module {top_module!r} is not in the netlist")
    if netlist_sha256 is None:
        netlist_sha256 = _sha256_json(yosys_data)
    _require_digest(netlist_sha256, "netlist")
    if source_sha256 is not None:
        _require_digest(source_sha256, "source")
    if constraints_sha256 is not None:
        _require_digest(constraints_sha256, "constraints")

    modules: dict[str, Any] = {}
    black_box_modules: list[str] = []
    unsupported_primitives: list[str] = []
    total_cells = 0
    total_nets = 0

    for name in sorted(modules_raw):
        if not isinstance(name, str) or not _MODULE_NAME.fullmatch(name):
            raise GateNetlistError(f"invalid module name {name!r}")
        raw = modules_raw[name]
        if not isinstance(raw, Mapping):
            raise GateNetlistError(f"module {name!r} must be an object")
        attributes = raw.get("attributes") or {}
        black_box = _truthy_attribute(attributes.get("blackbox")) \
            if isinstance(attributes, Mapping) else False

        raw_ports = raw.get("ports") or {}
        if not isinstance(raw_ports, Mapping):
            raise GateNetlistError(f"module {name!r} ports must be an object")
        for port_name in sorted(raw_ports):
            spec = raw_ports[port_name]
            if not isinstance(spec, Mapping):
                raise GateNetlistError(f"port {name}/{port_name} must be an object")
            direction = spec.get("direction")
            if direction not in _PORT_DIRECTIONS:
                raise GateNetlistError(
                    f"port {name}/{port_name} has invalid direction {direction!r}")
            _validate_bits(spec.get("bits"), f"port {name}/{port_name}")

        # Nets are identified by signal bit. Several netnames can alias one
        # bit; the artifact keeps every alias so search and cross-probing can
        # use either name, while ids stay stable.
        bit_to_net: dict[int, str] = {}
        net_names: dict[str, list[str]] = {}
        net_sources: dict[str, str | None] = {}

        def net_for_bit(bit: Any) -> str:
            if isinstance(bit, str):
                return f"c{bit}"
            net_id = bit_to_net.get(bit)
            if net_id is None:
                net_id = f"n{len(bit_to_net)}"
                bit_to_net[bit] = net_id
            return net_id

        ports = []
        # A module without netnames (a black box) has no net identity: port
        # membership stays unknown rather than fabricated from bit indices.
        has_net_identity = bool(raw.get("netnames")) or bool(raw.get("cells"))
        for port_name in sorted(raw_ports):
            spec = raw_ports[port_name]
            bits = _validate_bits(spec.get("bits"), f"port {name}/{port_name}")
            ports.append({"name": port_name, "direction": spec["direction"],
                          "width": len(bits),
                          "nets": [net_for_bit(bit) for bit in bits]
                          if has_net_identity else []})

        raw_netnames = raw.get("netnames") or {}
        if not isinstance(raw_netnames, Mapping):
            raise GateNetlistError(f"module {name!r} netnames must be an object")
        for net_name in sorted(raw_netnames):
            spec = raw_netnames[net_name]
            if not isinstance(spec, Mapping):
                raise GateNetlistError(f"net {name}/{net_name} must be an object")
            bits = _validate_bits(spec.get("bits"), f"net {name}/{net_name}")
            attrs = spec.get("attributes") or {}
            source = _clean_source(attrs.get("src")) if isinstance(attrs, Mapping) else None
            for bit in bits:
                net_id = net_for_bit(bit)
                if net_name not in net_names.setdefault(net_id, []):
                    net_names[net_id].append(net_name)
                if source and not net_sources.get(net_id):
                    net_sources[net_id] = source

        raw_cells = raw.get("cells") or {}
        if not isinstance(raw_cells, Mapping):
            raise GateNetlistError(f"module {name!r} cells must be an object")
        cells = []
        for cell_name in sorted(raw_cells):
            spec = raw_cells[cell_name]
            if not isinstance(spec, Mapping):
                raise GateNetlistError(f"cell {name}/{cell_name} must be an object")
            cell_type = spec.get("type")
            if not isinstance(cell_type, str) or not cell_type:
                raise GateNetlistError(f"cell {name}/{cell_name} has no type")
            directions = spec.get("port_directions")
            directions_known = isinstance(directions, Mapping) and bool(directions)
            if not directions_known and cell_type not in unsupported_primitives:
                unsupported_primitives.append(cell_type)
            connections: dict[str, list[str]] = {}
            raw_connections = spec.get("connections") or {}
            if not isinstance(raw_connections, Mapping):
                raise GateNetlistError(
                    f"cell {name}/{cell_name} connections must be an object")
            for port_name in sorted(raw_connections):
                bits = _validate_bits(
                    raw_connections[port_name],
                    f"cell {name}/{cell_name}/{port_name}")
                connections[port_name] = [net_for_bit(bit) for bit in bits]
                if directions_known and port_name not in directions:
                    directions_known = False
                    if cell_type not in unsupported_primitives:
                        unsupported_primitives.append(cell_type)
            attributes = spec.get("attributes") or {}
            source = _clean_source(attributes.get("src")) if isinstance(attributes, Mapping) else None
            cells.append({
                "name": cell_name,
                "type": cell_type,
                "ports": connections,
                "port_directions": dict(directions) if isinstance(directions, Mapping) else {},
                "directions_known": directions_known,
                "parameters": _plain_value(spec.get("parameters") or {}),
                "source": source,
                "hierarchy": None,  # resolved once all modules are known
            })

        nets = [{"id": net_id, "names": net_names[net_id],
                 "source": net_sources.get(net_id)} for net_id in net_names]
        # Constant nets exist only when a cell or port actually references
        # them; they are added on demand, never invented.
        referenced = {ref for cell in cells for refs in cell["ports"].values()
                      for ref in refs}
        referenced.update(f"c{bit}" for port_name in sorted(raw_ports)
                          for bit in _validate_bits(raw_ports[port_name]["bits"], "port")
                          if isinstance(bit, str))
        for const_id in sorted({ref for ref in referenced if ref.startswith("c")}):
            if const_id not in net_names:
                value = const_id[1:]
                nets.append({"id": const_id, "names": [f"<const {value}>"],
                             "source": None})
        nets.sort(key=lambda item: item["id"])
        if black_box and name not in black_box_modules:
            black_box_modules.append(name)
        total_cells += len(cells)
        total_nets += len(nets)
        modules[name] = {"ports": ports, "nets": nets, "cells": cells,
                         "black_box": black_box}

    # A cell is hierarchical only when its type is a real module of this
    # design. A module the tool marked as a black box (e.g. a standard cell
    # pulled in from liberty so pin directions are known) is a leaf, not
    # hierarchy; it is reported through the black-box fields instead.
    for module in modules.values():
        for cell in module["cells"]:
            child = modules.get(cell["type"])
            if child is not None and not child["black_box"]:
                cell["hierarchy"] = cell["type"]

    artifact = {
        "schema": SCHEMA_VERSION,
        "identity": {
            "job_id": job_id,
            "project": project,
            "source_sha256": source_sha256,
            "constraints_sha256": constraints_sha256,
            "netlist_sha256": netlist_sha256,
            "tool": tool,
            "tool_version": tool_version,
            "library": library,
            "top_module": top_module,
        },
        "modules": modules,
        "completeness": {
            "complete": not unsupported_primitives and not black_box_modules,
            "truncated": False,
            "truncated_reason": None,
            "unsupported_primitives": sorted(unsupported_primitives),
            "black_box_modules": sorted(black_box_modules),
            "unknown_direction_cells": sum(
                1 for module in modules.values() for cell in module["cells"]
                if not cell["directions_known"]),
            "module_count": len(modules),
            "cell_count": total_cells,
            "net_count": total_nets,
        },
    }
    # The body digest is part of the stored document; load_gate_netlist
    # recomputes it to detect any mutation of the persisted artifact.
    artifact["artifact_sha256"] = _sha256_json(artifact)
    return artifact


def load_gate_netlist(data: Any) -> dict[str, Any]:
    """Validate a stored artifact document, verifying its own digest.

    Rejects unsupported schema versions, duplicate ids, and pin/net or
    hierarchy references that point outside the artifact.
    """
    if not isinstance(data, Mapping):
        raise GateNetlistError("gate netlist artifact must be an object")
    if data.get("schema") != SCHEMA_VERSION:
        raise GateNetlistError(
            f"unsupported gate netlist schema {data.get('schema')!r}")
    artifact = dict(data)
    declared = artifact.get("artifact_sha256")
    _require_digest(declared, "artifact")
    body = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
    if _sha256_json(body) != declared:
        raise GateNetlistError("artifact digest does not match its content")
    identity = artifact.get("identity")
    if not isinstance(identity, Mapping):
        raise GateNetlistError("artifact identity must be an object")
    modules = artifact.get("modules")
    if not isinstance(modules, Mapping) or not modules:
        raise GateNetlistError("artifact must define at least one module")
    if identity.get("top_module") not in modules:
        raise GateNetlistError("artifact top module is not defined")
    seen_cells: set[str] = set()
    for name, module in modules.items():
        if not isinstance(name, str) or not _MODULE_NAME.fullmatch(name):
            raise GateNetlistError(f"invalid module name {name!r}")
        if not isinstance(module, Mapping):
            raise GateNetlistError(f"module {name!r} must be an object")
        nets = module.get("nets")
        cells = module.get("cells")
        ports = module.get("ports")
        for field in (nets, cells, ports):
            if not isinstance(field, list):
                raise GateNetlistError(f"module {name!r} fields must be lists")
        net_ids = [item.get("id") for item in nets if isinstance(item, Mapping)]
        if len(net_ids) != len(nets) or len(set(net_ids)) != len(net_ids) \
                or any(not isinstance(v, str) or not v for v in net_ids):
            raise GateNetlistError(f"module {name!r} has duplicate or invalid net ids")
        cell_names = [item.get("name") for item in cells if isinstance(item, Mapping)]
        if len(cell_names) != len(cells) or len(set(cell_names)) != len(cell_names) \
                or any(not isinstance(v, str) or not v for v in cell_names):
            raise GateNetlistError(f"module {name!r} has duplicate or invalid cell ids")
        port_names = [item.get("name") for item in ports if isinstance(item, Mapping)]
        if len(port_names) != len(ports) or len(set(port_names)) != len(port_names) \
                or any(not isinstance(v, str) or not v for v in port_names):
            raise GateNetlistError(f"module {name!r} has duplicate or invalid port ids")
        valid_nets = set(net_ids)
        for port in ports:
            port_refs = port.get("nets")
            if not isinstance(port_refs, list) or any(ref not in valid_nets for ref in port_refs):
                raise GateNetlistError(
                    f"port {name}/{port.get('name')!r} references an unknown net")
        for cell in cells:
            cell_id = f"{name}/{cell.get('name')}"
            if cell_id in seen_cells:
                raise GateNetlistError(f"duplicate cell id {cell_id!r}")
            seen_cells.add(cell_id)
            ports_map = cell.get("ports")
            if not isinstance(ports_map, Mapping):
                raise GateNetlistError(f"cell {cell_id} ports must be an object")
            for pin, refs in ports_map.items():
                if not isinstance(refs, list) or any(ref not in valid_nets for ref in refs):
                    raise GateNetlistError(
                        f"cell {cell_id} pin {pin!r} references an unknown net")
            child = cell.get("hierarchy")
            if child is not None and child not in modules:
                raise GateNetlistError(
                    f"cell {cell_id} hierarchy references unknown module {child!r}")
    return artifact


def artifact_summary(artifact: Mapping, *, repository_sha256: str | None = None,
                     size_bytes: int | None = None) -> dict[str, Any]:
    """Job-facing metadata: availability, digests, counts, completeness."""
    identity = artifact.get("identity") or {}
    completeness = artifact.get("completeness") or {}
    summary = {
        "schema": artifact.get("schema"),
        "available": True,
        "artifact_sha256": artifact.get("artifact_sha256"),
        "repository_sha256": repository_sha256,
        "size_bytes": size_bytes,
        "job_id": identity.get("job_id"),
        "project": identity.get("project"),
        "source_sha256": identity.get("source_sha256"),
        "constraints_sha256": identity.get("constraints_sha256"),
        "netlist_sha256": identity.get("netlist_sha256"),
        "tool": identity.get("tool"),
        "tool_version": identity.get("tool_version"),
        "library": identity.get("library"),
        "top_module": identity.get("top_module"),
        "module_count": completeness.get("module_count"),
        "cell_count": completeness.get("cell_count"),
        "net_count": completeness.get("net_count"),
        "completeness": completeness,
        "modules": sorted((artifact.get("modules") or {}).keys()),
    }
    return summary


def unavailable(reason: str) -> dict[str, Any]:
    """Honest absence: no artifact, no diagram."""
    return {"schema": SCHEMA_VERSION, "available": False, "reason": reason}


# -- Bounded graph views ------------------------------------------------------


def bounded_graph(artifact: Mapping, *, module: str | None = None,
                  depth: int = 1, node_limit: int = 500,
                  cone: str | None = None,
                  cone_hops: int = 6) -> dict[str, Any]:
    """Return a bounded, deterministic view of one hierarchy scope.

    The view contains the scope's ports and cells (capped at ``node_limit``),
    the nets among them with driver/load pin references, and — for depth > 1 —
    the inlined contents of child modules, still under the same node limit.
    ``cone`` restricts the view to the transitive fan-in/fan-out of one cell
    or net of the scope module, bounded by ``cone_hops``; a cone root is
    always scope-local, never an inlined child id.
    """
    if not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= MAX_DEPTH:
        raise GateNetlistError(f"depth must be an integer in 1..{MAX_DEPTH}")
    if not isinstance(node_limit, int) or isinstance(node_limit, bool) \
            or not 1 <= node_limit <= MAX_NODE_LIMIT:
        raise GateNetlistError(f"node limit must be an integer in 1..{MAX_NODE_LIMIT}")
    if cone is not None and (not isinstance(cone, str) or not cone or len(cone) > 512):
        raise GateNetlistError("cone root must be a non-empty id")
    if not isinstance(cone_hops, int) or isinstance(cone_hops, bool) \
            or not 1 <= cone_hops <= MAX_CONE_HOPS:
        raise GateNetlistError(f"cone hops must be an integer in 1..{MAX_CONE_HOPS}")

    modules = artifact.get("modules") or {}
    scope = module if module is not None else (artifact.get("identity") or {}).get("top_module")
    if not isinstance(scope, str) or scope not in modules:
        raise GateNetlistError(f"unknown module scope {scope!r}")

    included_cells: dict[str, dict[str, Any]] = {}   # scoped cell id -> cell
    included_ports: dict[str, dict[str, Any]] = {}
    truncated_reasons: list[str] = []

    def add_scope(prefix: str, module_name: str, budget: dict[str, int]) -> None:
        data = modules[module_name]
        for port in data["ports"]:
            key = f"port:{prefix}{port['name']}"
            if key in included_ports:
                continue
            if budget["nodes"] <= 0:
                truncated_reasons.append("node limit reached while adding ports")
                continue
            included_ports[key] = {**port, "module": module_name}
            budget["nodes"] -= 1
        for cell in data["cells"]:
            key = f"cell:{prefix}{cell['name']}"
            if key in included_cells:
                continue
            if budget["nodes"] <= 0:
                truncated_reasons.append("node limit reached while adding cells")
                return
            included_cells[key] = {**cell, "module": module_name}
            budget["nodes"] -= 1

    budget = {"nodes": node_limit}
    add_scope("", scope, budget)
    # Depth > 1 inlines child hierarchy levels breadth-first under the limit.
    if depth > 1:
        frontier = [(cell_id, cell) for cell_id, cell in included_cells.items()
                    if cell.get("hierarchy")]
        seen_modules = {scope}
        for _level in range(depth - 1):
            next_frontier = []
            for parent_id, parent in frontier:
                child = parent.get("hierarchy")
                if child in seen_modules or child not in modules:
                    continue
                seen_modules.add(child)
                prefix = f"{parent_id[5:]}/"
                add_scope(prefix, child, budget)
                next_frontier.extend(
                    (cell_id, cell) for cell_id, cell in included_cells.items()
                    if cell.get("hierarchy") and cell_id.startswith(f"cell:{prefix}"))
            frontier = next_frontier
            if not frontier or budget["nodes"] <= 0:
                break

    # Cone restriction: BFS over driver/load links from the root, within the
    # scope module. Unknown roots raise instead of returning an empty graph.
    if cone is not None:
        included_cells, included_ports = _restrict_cone(
            modules[scope], cone, cone_hops, included_cells, included_ports,
            scope=scope)

    nodes = []
    for port_id, port in sorted(included_ports.items()):
        nodes.append({"id": port_id, "kind": "port", "name": port["name"],
                      "direction": port["direction"], "width": port["width"],
                      "nets": port.get("nets") or [],
                      "module": port["module"]})
    for cell_id, cell in sorted(included_cells.items()):
        nodes.append({
            "id": cell_id, "kind": "cell", "name": cell["name"],
            "type": cell["type"], "module": cell["module"],
            "hierarchy": cell.get("hierarchy"),
            "directions_known": bool(cell.get("directions_known")),
            "black_box": bool((modules.get(cell["type"]) or {}).get("black_box")),
            "source": cell.get("source"),
            "pins": {pin: sorted(set(refs))
                     for pin, refs in (cell.get("ports") or {}).items()},
            "pin_directions": (cell.get("port_directions") or {})
            if cell.get("directions_known") else {},
        })
    cell_by_id = {cell_id: cell for cell_id, cell in included_cells.items()}
    node_ids = {node["id"] for node in nodes}

    # Pin references per net, module-scoped; directions are never guessed.
    # A module input port is driven from outside the module, an output port
    # is a load of internal drivers; inout participates on both sides.
    net_pins: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for node in nodes:
        if node["kind"] == "port":
            for ref in sorted(set(node.get("nets") or [])):
                entry = net_pins.setdefault(f"{node['module']}:{ref}",
                                            {"drivers": [], "loads": []})
                if node["direction"] in ("input", "inout"):
                    entry["drivers"].append({"node": node["id"], "pin": None})
                if node["direction"] in ("output", "inout"):
                    entry["loads"].append({"node": node["id"], "pin": None})
            continue
        cell = cell_by_id[node["id"]]
        directions = (cell.get("port_directions") or {}) if cell.get("directions_known") else {}
        for pin, refs in (cell.get("ports") or {}).items():
            direction = directions.get(pin)
            for ref in sorted(set(refs)):
                entry = net_pins.setdefault(f"{cell['module']}:{ref}",
                                            {"drivers": [], "loads": []})
                pin_ref = {"node": node["id"], "pin": pin}
                if direction == "output":
                    entry["drivers"].append(pin_ref)
                elif direction == "input":
                    entry["loads"].append(pin_ref)
                # Unknown direction: recorded on neither side, never guessed.

    net_lookup = {f"{module_name}:{net['id']}": {**net, "module": module_name}
                  for module_name in sorted({node["module"] for node in nodes})
                  for net in modules[module_name]["nets"]}

    nets = []
    edges = []
    for key, pins in sorted(net_pins.items()):
        net = net_lookup.get(key)
        if net is None or (not pins["drivers"] and not pins["loads"]):
            continue
        net_id = f"net:{key}"
        names = net.get("names") or [net["id"]]
        nets.append({
            "id": net_id, "module": net["module"], "name": names[0],
            "names": names, "source": net.get("source"),
            "drivers": pins["drivers"], "loads": pins["loads"],
        })
        for driver in pins["drivers"]:
            for load in pins["loads"]:
                if driver["node"] in node_ids and load["node"] in node_ids:
                    edges.append({"net": net_id, "from": driver, "to": load})

    identity = artifact.get("identity") or {}
    truncated = bool(truncated_reasons)
    return {
        "schema": GRAPH_SCHEMA_VERSION,
        "job_id": identity.get("job_id"),
        "artifact_sha256": artifact.get("artifact_sha256"),
        "netlist_sha256": identity.get("netlist_sha256"),
        "source_sha256": identity.get("source_sha256"),
        "constraints_sha256": identity.get("constraints_sha256"),
        "top_module": identity.get("top_module"),
        "module": scope,
        "depth": depth,
        "node_limit": node_limit,
        "cone": cone,
        "modules": sorted(modules.keys()),
        "nodes": nodes,
        "nets": nets,
        "edges": edges,
        "truncated": truncated,
        "truncated_reasons": sorted(set(truncated_reasons)),
        "counts": {
            "nodes": len(nodes),
            "nets": len(nets),
            "cells": sum(1 for node in nodes if node["kind"] == "cell"),
            "hierarchical": sum(1 for node in nodes if node.get("hierarchy")),
        },
        "completeness": artifact.get("completeness"),
    }


def _restrict_cone(module: Mapping, cone: str, cone_hops: int,
                   included_cells: dict[str, dict[str, Any]],
                   included_ports: dict[str, dict[str, Any]],
                   scope: str = ""):
    """Restrict a scope view to the fan-in/fan-out cone of one root id."""
    cell_nets: dict[str, set[str]] = {}
    net_cells: dict[str, set[str]] = {}
    for cell in module["cells"]:
        refs = {ref for refs in (cell.get("ports") or {}).values() for ref in refs}
        cell_nets[cell["name"]] = refs
        for ref in refs:
            net_cells.setdefault(ref, set()).add(cell["name"])
    port_nets = {port["name"]: set(port.get("nets") or []) for port in module["ports"]}

    root_cells: set[str] = set()
    keep_port: str | None = None
    if cone.startswith("cell:"):
        name = cone[5:]
        if name not in cell_nets:
            raise GateNetlistError(f"cone root {cone!r} is not a cell in this scope")
        root_cells.add(name)
    elif cone.startswith("port:"):
        name = cone[5:]
        if name not in port_nets:
            raise GateNetlistError(f"cone root {cone!r} is not a port in this scope")
        keep_port = cone
        for ref in port_nets[name]:
            root_cells.update(net_cells.get(ref, set()))
    else:
        net = next((item for item in module["nets"]
                    if item["id"] == cone or cone in (item.get("names") or [])), None)
        if net is None:
            raise GateNetlistError(f"cone root {cone!r} is not a net in this scope")
        root_cells = set(net_cells.get(net["id"], set()))

    visited: set[str] = set()
    frontier = set(root_cells)
    hops = 0
    while frontier and hops < cone_hops:
        next_frontier: set[str] = set()
        for cell_name in frontier:
            if cell_name in visited:
                continue
            visited.add(cell_name)
            for ref in cell_nets.get(cell_name, set()):
                next_frontier.update(net_cells.get(ref, set()) - visited)
        frontier = next_frontier
        hops += 1

    def touches_cone(cell_id: str, cell: Mapping) -> bool:
        # Scope-level cells match by bare name; inlined child ids keep the
        # parent instance prefix and are kept only if their parent is in the
        # cone (the cone itself is scope-local by contract).
        name = cell_id[5:]
        if name in visited:
            return True
        parent = name.split("/", 1)[0] if "/" in name else None
        return parent in visited

    cone_cells = {cell_id: cell for cell_id, cell in included_cells.items()
                  if touches_cone(cell_id, cell)}
    cone_ports = {port_id: port for port_id, port in included_ports.items()
                  if _port_touches(module, port, visited)}
    if keep_port is not None and keep_port not in cone_ports:
        for port in module["ports"]:
            if port["name"] == cone[5:]:
                cone_ports[keep_port] = {**port, "module": scope}
    return cone_cells, cone_ports


def _port_touches(module: Mapping, port: Mapping, cells: set[str]) -> bool:
    """True when the port's nets are referenced by a cone cell (or the port is
    itself the cone root's boundary)."""
    port_nets = set(port.get("nets") or [])
    if not port_nets:
        return False
    for cell_name in cells:
        for cell in module["cells"]:
            if cell["name"] != cell_name:
                continue
            refs = {ref for refs in (cell.get("ports") or {}).values() for ref in refs}
            if refs & port_nets:
                return True
    return False


# -- Job integration ----------------------------------------------------------


def build_job_netlist_manifest(yosys_path: str, *, top_module: str,
                               job_id: str | None = None,
                               project: str | None = None,
                               source_sha256: str | None = None,
                               constraints_sha256: str | None = None,
                               library: str | None = None) -> dict[str, Any]:
    """Build and validate the artifact for one synthesis job's netlist output.

    Returns an ``unavailable`` record (never raises) when the structured
    netlist output is missing or unreadable, so a job without structured
    synthesis output stays honest without failing the synthesis itself.
    The full artifact is embedded under ``artifact`` for the caller to
    persist through the evidence store.
    """
    try:
        if not os.path.isfile(yosys_path):
            return unavailable("no structured netlist output was produced by this job")
        if os.path.getsize(yosys_path) > MAX_YOSYS_JSON_BYTES:
            return unavailable("structured netlist output exceeds the size bound")
        with open(yosys_path, encoding="utf-8") as handle:
            raw = handle.read()
        data = json.loads(raw)
        artifact = build_gate_netlist(
            data, top_module=top_module, job_id=job_id, project=project,
            source_sha256=source_sha256, constraints_sha256=constraints_sha256,
            netlist_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            tool="yosys", library=library)
        artifact = load_gate_netlist(artifact)
        content = canonical_bytes(artifact)
        summary = artifact_summary(artifact,
                                   repository_sha256=hashlib.sha256(content).hexdigest(),
                                   size_bytes=len(content))
        summary["artifact"] = artifact
        return summary
    except (OSError, ValueError, TypeError) as exc:
        return unavailable(f"structured netlist output rejected: {exc}")
