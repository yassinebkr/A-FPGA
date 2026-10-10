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

from gate_netlist_contract import (
    GateNetlistError,
    GRAPH_SCHEMA_VERSION,
    MAX_CONE_HOPS,
    MAX_DEPTH,
    MAX_NODE_LIMIT,
    MAX_YOSYS_JSON_BYTES,
    SCHEMA_VERSION,
    _MODULE_NAME,
    _PORT_DIRECTIONS,
    canonical_bytes,
    canonical_json as _canonical_json,
    clean_source as _clean_source,
    plain_value as _plain_value,
    require_digest as _require_digest,
    sha256_json as _sha256_json,
    truthy_attribute as _truthy_attribute,
    validate_bits as _validate_bits,
)
from gate_netlist_schema import (
    artifact_summary,
    load_gate_netlist,
    unavailable,
)
from gate_netlist_query import bounded_graph


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
