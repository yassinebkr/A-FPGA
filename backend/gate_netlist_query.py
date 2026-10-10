"""Bounded graph queries over a validated gate-netlist artifact."""

from __future__ import annotations

from typing import Any, Mapping

from gate_netlist_contract import (
    GateNetlistError,
    GRAPH_SCHEMA_VERSION,
    MAX_CONE_HOPS,
    MAX_DEPTH,
    MAX_NODE_LIMIT,
)


def bounded_graph(artifact: Mapping, *, module: str | None = None,
                  depth: int = 1, node_limit: int = 500,
                  cone: str | None = None,
                  cone_hops: int = 6) -> dict[str, Any]:
    """Return a deterministic, bounded hierarchy/cone view.

    The caller is expected to pass a document already accepted by
    ``load_gate_netlist``.  The query still validates all request bounds and
    scope references so REST callers cannot turn a valid artifact into an
    unbounded traversal.
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

    included_cells: dict[str, dict[str, Any]] = {}
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

    if cone is not None:
        included_cells, included_ports = _restrict_cone(
            modules[scope], cone, cone_hops, included_cells, included_ports,
            scope=scope)

    nodes = []
    for port_id, port in sorted(included_ports.items()):
        nodes.append({"id": port_id, "kind": "port", "name": port["name"],
                      "direction": port["direction"], "width": port["width"],
                      "nets": port.get("nets") or [], "module": port["module"]})
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

    # A bounded graph may select a few hundred cells from a multi-million-net
    # module.  Index only the nets referenced by those selected nodes, and
    # stop scanning a deterministically ordered net list once every reference
    # has been resolved.  This keeps the response bound meaningful for large
    # artifacts while retaining the existing fail-closed behavior for a
    # malformed reference (the net is simply omitted below).
    needed_net_keys = set(net_pins)
    net_lookup: dict[str, dict[str, Any]] = {}
    for module_name in sorted({node["module"] for node in nodes}):
        for net in modules[module_name]["nets"]:
            key = f"{module_name}:{net['id']}"
            if key in needed_net_keys:
                net_lookup[key] = {**net, "module": module_name}
                if len(net_lookup) == len(needed_net_keys):
                    break
        if len(net_lookup) == len(needed_net_keys):
            break

    nets = []
    edges = []
    for key, pins in sorted(net_pins.items()):
        net = net_lookup.get(key)
        if net is None or (not pins["drivers"] and not pins["loads"]):
            continue
        net_id = f"net:{key}"
        names = net.get("names") or [net["id"]]
        nets.append({"id": net_id, "module": net["module"], "name": names[0],
                     "names": names, "source": net.get("source"),
                     "drivers": pins["drivers"], "loads": pins["loads"]})
        for driver in pins["drivers"]:
            for load in pins["loads"]:
                if driver["node"] in node_ids and load["node"] in node_ids:
                    edges.append({"net": net_id, "from": driver, "to": load})

    identity = artifact.get("identity") or {}
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
        "truncated": bool(truncated_reasons),
        "truncated_reasons": sorted(set(truncated_reasons)),
        "counts": {"nodes": len(nodes), "nets": len(nets),
                   "cells": sum(1 for node in nodes if node["kind"] == "cell"),
                   "hierarchical": sum(1 for node in nodes if node.get("hierarchy"))},
        "completeness": artifact.get("completeness"),
    }


def _restrict_cone(module: Mapping, cone: str, cone_hops: int,
                   included_cells: dict[str, dict[str, Any]],
                   included_ports: dict[str, dict[str, Any]],
                   scope: str = ""):
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

    def touches_cone(cell_id: str) -> bool:
        name = cell_id[5:]
        if name in visited:
            return True
        parent = name.split("/", 1)[0] if "/" in name else None
        return parent in visited

    cone_cells = {cell_id: cell for cell_id, cell in included_cells.items()
                  if touches_cone(cell_id)}
    cone_ports = {port_id: port for port_id, port in included_ports.items()
                  if _port_touches(module, port, visited)}
    if keep_port is not None and keep_port not in cone_ports:
        for port in module["ports"]:
            if port["name"] == cone[5:]:
                cone_ports[keep_port] = {**port, "module": scope}
    return cone_cells, cone_ports


def _port_touches(module: Mapping, port: Mapping, cells: set[str]) -> bool:
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
