"""Validation and API summaries for persisted gate-netlist artifacts.

The builder owns Yosys normalization.  This module owns the persisted
document contract so loading evidence never depends on parser implementation
details and always verifies the artifact's self-digest before traversal.
"""

from __future__ import annotations

from typing import Any, Mapping

from gate_netlist_contract import (
    GateNetlistError, SCHEMA_VERSION, _MODULE_NAME, require_digest, sha256_json
)


def load_gate_netlist(data: Any) -> dict[str, Any]:
    """Validate a stored artifact document and verify its own digest."""
    if not isinstance(data, Mapping):
        raise GateNetlistError("gate netlist artifact must be an object")
    if data.get("schema") != SCHEMA_VERSION:
        raise GateNetlistError(
            f"unsupported gate netlist schema {data.get('schema')!r}")
    artifact = dict(data)
    declared = artifact.get("artifact_sha256")
    require_digest(declared, "artifact")
    body = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
    if sha256_json(body) != declared:
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
        nets, cells, ports = (module.get(field) for field in ("nets", "cells", "ports"))
        for field in (nets, cells, ports):
            if not isinstance(field, list):
                raise GateNetlistError(f"module {name!r} fields must be lists")
        net_ids = [item.get("id") for item in nets if isinstance(item, Mapping)]
        if (len(net_ids) != len(nets) or len(set(net_ids)) != len(net_ids)
                or any(not isinstance(v, str) or not v for v in net_ids)):
            raise GateNetlistError(f"module {name!r} has duplicate or invalid net ids")
        cell_names = [item.get("name") for item in cells if isinstance(item, Mapping)]
        if (len(cell_names) != len(cells) or len(set(cell_names)) != len(cell_names)
                or any(not isinstance(v, str) or not v for v in cell_names)):
            raise GateNetlistError(f"module {name!r} has duplicate or invalid cell ids")
        port_names = [item.get("name") for item in ports if isinstance(item, Mapping)]
        if (len(port_names) != len(ports) or len(set(port_names)) != len(port_names)
                or any(not isinstance(v, str) or not v for v in port_names)):
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
    """Return stable job/API metadata without exposing the full artifact."""
    identity = artifact.get("identity") or {}
    completeness = artifact.get("completeness") or {}
    return {
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


def unavailable(reason: str) -> dict[str, Any]:
    """Represent missing evidence without fabricating a graph."""
    if not isinstance(reason, str) or not reason.strip():
        reason = "gate netlist artifact is unavailable"
    return {"schema": SCHEMA_VERSION, "available": False, "reason": reason}

