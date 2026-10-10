"""Shared contracts for persisted visualization artifacts.

The gate, timing, and physical views have different schemas and validators,
but they share two wire-level rules: canonical JSON is the persisted byte
representation and the artifact digest covers every field except its own
digest.  Keeping those mechanics here prevents the individual parsers from
silently drifting apart while leaving schema-specific checks in their owning
modules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Type

from gate_netlist import canonical_bytes, _sha256_json

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def unavailable(schema: str, reason: str) -> dict[str, Any]:
    """Return the stable, honest representation of a missing artifact."""
    return {"schema": schema, "available": False, "reason": reason}


def seal(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Copy an artifact and add its deterministic self-digest."""
    body = {key: value for key, value in artifact.items()
            if key != "artifact_sha256"}
    body["artifact_sha256"] = _sha256_json(body)
    return body


def load_sealed(data: Any, *, schema: str, label: str,
                error_type: Type[ValueError]) -> dict[str, Any]:
    """Validate the common envelope and self-digest of a stored artifact.

    Collection shape and field semantics remain with the timing/layout
    modules.  ``error_type`` keeps their public exception classes stable.
    """
    if not isinstance(data, Mapping):
        raise error_type(f"{label} must be an object")
    if data.get("schema") != schema:
        raise error_type(f"unsupported {label} schema {data.get('schema')!r}")
    artifact = dict(data)
    declared = artifact.get("artifact_sha256")
    if not isinstance(declared, str) or not _SHA256.fullmatch(declared):
        raise error_type(f"{label} digest is invalid")
    body = {key: value for key, value in artifact.items()
            if key != "artifact_sha256"}
    if _sha256_json(body) != declared:
        raise error_type(f"{label} digest does not match its content")
    return artifact


def netlist_identity(netlist_artifact: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the shared identity fields carried by derived views.

    The function deliberately does not validate the netlist schema.  The
    gate-netlist module owns that contract; callers validate it when they load
    the source artifact.  This helper only makes derived-artifact identity
    construction consistent.
    """
    if not isinstance(netlist_artifact, Mapping):
        return {}
    identity = netlist_artifact.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    return {
        "netlist_sha256": identity.get("netlist_sha256"),
        "gate_artifact_sha256": netlist_artifact.get("artifact_sha256"),
        "source_sha256": identity.get("source_sha256"),
        "constraints_sha256": identity.get("constraints_sha256"),
        "top_module": identity.get("top_module"),
    }


__all__ = [
    "canonical_bytes",
    "load_sealed",
    "netlist_identity",
    "seal",
    "unavailable",
]
