"""Shared contracts for canonical gate-netlist artifacts.

This module contains the small, dependency-free identity and primitive-value
rules used by the parser, artifact validator, and graph query layer.  Keeping
these rules in one place prevents the persisted artifact and query code from
silently developing different digest or input-validation semantics.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

SCHEMA_VERSION = "gate-netlist/v1"
GRAPH_SCHEMA_VERSION = "gate-graph/v1"

# Public request/file bounds.  Callers must explicitly request a bounded view.
MAX_NODE_LIMIT = 2000
MAX_DEPTH = 4
MAX_CONE_HOPS = 16
MAX_YOSYS_JSON_BYTES = 256 * 1024 * 1024

_BIT_CONSTANTS = frozenset({"0", "1", "x", "z"})
_PORT_DIRECTIONS = frozenset({"input", "output", "inout"})
_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GateNetlistError(ValueError):
    """Invalid netlist data, artifact identity, or graph request."""


def canonical_json(value: Any) -> str:
    """Return the deterministic JSON representation used by all digests."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def canonical_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 bytes for persistence and digesting."""
    return canonical_json(value).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def require_digest(value: Any, label: str, *, optional: bool = False) -> str | None:
    if value is None:
        if optional:
            return None
        raise GateNetlistError(f"{label} digest is required")
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise GateNetlistError(f"{label} must be a SHA-256 digest")
    return value


def validate_bits(bits: Any, label: str) -> list[Any]:
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


def plain_value(value: Any) -> Any:
    """Keep only JSON-primitive parameter values, deterministically."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): plain_value(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [plain_value(v) for v in value]
    return str(value)


def clean_source(value: Any) -> str | None:
    """Keep a bounded tool-supplied source attribute."""
    if isinstance(value, str) and 0 < len(value) < 512:
        return value
    return None


def truthy_attribute(value: Any) -> bool:
    """Decode Yosys boolean attributes, including ASCII bit-vectors."""
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


# Compatibility aliases for existing backend modules that imported these
# helpers before C3 extracted the contract layer.
_canonical_json = canonical_json
_sha256_json = sha256_json
_MODULE_NAME_RE = _MODULE_NAME

