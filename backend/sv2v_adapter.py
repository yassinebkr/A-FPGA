"""Deterministic, reviewed post-sv2v adapter transforms.

sv2v converts SystemVerilog unpacked arrays to a mix of packed vectors (for
ports) and unpacked arrays (for internal signals).  Some assignments between
these representations are rejected by Slang/Verilator because they require
implicit unpacked-to-packed conversion.

This module applies a small, explicit, reviewable set of transforms that
preserve the exact semantics of the original SystemVerilog while producing
Verilog that elaborates cleanly through Slang and Yosys.  Every transform is
deterministic, idempotent, and recorded in the evidence report.

Transforms:

1. ``expand_2d_array_port_assign`` — replaces
   ``assign packed_port = array2d[IDX];`` with an explicit concatenation
   ``assign packed_port = {array2d[IDX][N-1], ..., array2d[IDX][0]};``

2. ``expand_2d_array_stage_copy`` — replaces
   ``array2d[i] <= array2d[i-1];`` with per-element copies
   ``array2d[i][j] <= array2d[i-1][j];`` for each j.

The adapter never modifies the upstream RTL.  It operates only on the sv2v
output artifact, and the transform is recorded with its own digest so the
result is fully auditable.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any


SCHEMA = "sv2v-adapter-report/v1"


@dataclass(frozen=True)
class AdapterTransform:
    """One recorded transform application."""

    name: str
    pattern: str
    replacement: str
    line_number: int
    context_before: str
    context_after: str


@dataclass
class AdapterResult:
    """Result of applying adapter transforms to sv2v output."""

    source_sha256: str
    output_sha256: str
    output_text: str
    transforms_applied: list[AdapterTransform] = field(default_factory=list)
    schema: str = SCHEMA

    def report(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "source_sha256": self.source_sha256,
            "output_sha256": self.output_sha256,
            "transforms_applied": [
                {
                    "name": t.name,
                    "pattern": t.pattern,
                    "replacement": t.replacement,
                    "line_number": t.line_number,
                    "context_before": t.context_before,
                    "context_after": t.context_after,
                }
                for t in self.transforms_applied
            ],
            "transform_count": len(self.transforms_applied),
        }


class Sv2vAdapterError(RuntimeError):
    """Raised when an adapter transform cannot be applied safely."""


# Pattern: assign <packed_signal> = <array2d>[<expr>];
# where array2d is declared as reg [W] name [0:X][0:Y]
_2D_ARRAY_DECL = re.compile(
    r"^\s*reg\s+\[(\d+):(\d+)\]\s+(\w+)\s*\[0:([^\]]+)\]\s*\[0:(\d+)\]\s*;",
    re.MULTILINE,
)

# Pattern: assign <target> = <array2d>[<idx>];
_PORT_ASSIGN = re.compile(
    r"^(\s*)assign\s+(\w+)\s*=\s*(\w+)\s*\[([^\]]+)\]\s*;",
    re.MULTILINE,
)

# Pattern: <array2d>[i] <= <array2d>[i-1];  (stage copy)
_STAGE_COPY = re.compile(
    r"^(\s*)(\w+)\s*\[(\w+)\]\s*<=\s*(\w+)\s*\[\s*\3\s*-\s*1\s*\]\s*;",
    re.MULTILINE,
)


def _find_2d_arrays(text: str) -> dict[str, dict[str, Any]]:
    """Find all 2D unpacked array declarations: reg [W] name [0:X][0:Y]."""
    arrays: dict[str, dict[str, Any]] = {}
    for match in _2D_ARRAY_DECL.finditer(text):
        msb, lsb, name, dim1, dim2 = match.groups()
        arrays[name] = {
            "msb": int(msb),
            "lsb": int(lsb),
            "width": int(msb) - int(lsb) + 1,
            "dim1": dim1.strip(),
            "dim2_count": int(dim2) + 1,
        }
    return arrays


def _expand_port_assign(
    text: str,
    arrays: dict[str, dict[str, Any]],
    transforms: list[AdapterTransform],
) -> str:
    """Expand: assign packed = array2d[IDX]; → assign packed = {array2d[IDX][N-1], ..., array2d[IDX][0]};"""
    lines = text.split("\n")
    result_lines: list[str] = []
    for line_num, line in enumerate(lines, start=1):
        match = _PORT_ASSIGN.match(line)
        if match:
            indent, target, source, idx = match.groups()
            if source in arrays:
                info = arrays[source]
                n = info["dim2_count"]
                # Build explicit concatenation: {arr[IDX][N-1], arr[IDX][N-2], ..., arr[IDX][0]}
                parts = [f"{source}[{idx}][{j}]" for j in range(n - 1, -1, -1)]
                concat = "{" + ", ".join(parts) + "}"
                new_line = f"{indent}assign {target} = {concat};"
                transforms.append(AdapterTransform(
                    name="expand_2d_array_port_assign",
                    pattern=line.strip(),
                    replacement=new_line.strip(),
                    line_number=line_num,
                    context_before=line.strip(),
                    context_after=new_line.strip(),
                ))
                result_lines.append(new_line)
                continue
        result_lines.append(line)
    return "\n".join(result_lines)


def _expand_stage_copy(
    text: str,
    arrays: dict[str, dict[str, Any]],
    transforms: list[AdapterTransform],
) -> str:
    """Expand: array2d[i] <= array2d[i-1]; → per-element copies."""
    lines = text.split("\n")
    result_lines: list[str] = []
    for line_num, line in enumerate(lines, start=1):
        match = _STAGE_COPY.match(line)
        if match:
            indent, target, idx_var, source = match.groups()
            if target in arrays and target == source:
                info = arrays[target]
                n = info["dim2_count"]
                for j in range(n):
                    new_line = f"{indent}{target}[{idx_var}][{j}] <= {source}[{idx_var} - 1][{j}];"
                    transforms.append(AdapterTransform(
                        name="expand_2d_array_stage_copy",
                        pattern=line.strip(),
                        replacement=new_line.strip(),
                        line_number=line_num,
                        context_before=line.strip(),
                        context_after=new_line.strip(),
                    ))
                    result_lines.append(new_line)
                continue
        result_lines.append(line)
    return "\n".join(result_lines)


def apply_adapter(text: str) -> AdapterResult:
    """Apply all deterministic adapter transforms to sv2v output.

    The transforms are applied in a fixed order and are idempotent.
    Returns an AdapterResult with the transformed text and a full record
    of every transform applied.
    """
    source_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    transforms: list[AdapterTransform] = []

    arrays = _find_2d_arrays(text)
    if not arrays:
        return AdapterResult(
            source_sha256=source_sha,
            output_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            output_text=text,
            transforms_applied=[],
        )

    result = text
    result = _expand_port_assign(result, arrays, transforms)
    result = _expand_stage_copy(result, arrays, transforms)

    output_sha = hashlib.sha256(result.encode("utf-8")).hexdigest()
    return AdapterResult(
        source_sha256=source_sha,
        output_sha256=output_sha,
        output_text=result,
        transforms_applied=transforms,
    )


def apply_adapter_idempotent(text: str) -> AdapterResult:
    """Apply adapter and verify idempotency (second pass is a no-op)."""
    first = apply_adapter(text)
    second = apply_adapter(first.output_text)
    if second.output_sha256 != first.output_sha256:
        raise Sv2vAdapterError(
            "adapter is not idempotent: second pass changed the output"
        )
    if second.transforms_applied:
        raise Sv2vAdapterError(
            f"adapter is not idempotent: second pass applied {len(second.transforms_applied)} transforms"
        )
    return first


__all__ = [
    "AdapterResult",
    "AdapterTransform",
    "SCHEMA",
    "Sv2vAdapterError",
    "apply_adapter",
    "apply_adapter_idempotent",
]
