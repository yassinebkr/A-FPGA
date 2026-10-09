"""Deterministic, digest-visible specialization of the pinned Ibex top."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from hardware_project import HardwareProjectUpdate


class IbexSpecializationError(ValueError):
    """Raised when the pinned top no longer matches the reviewed declaration."""


_VALUE_MAP: dict[str, Mapping[Any, str]] = {
    "RV32E": {0: "1'b0", False: "1'b0"},
    "RV32M": {"none": "RV32MNone"},
    "RV32B": {"none": "RV32BNone"},
    # This revision cannot disable compressed instructions completely.
    "RV32ZC": {"zca": "RV32Zca"},
    "DbgTriggerEn": {0: "1'b0", False: "1'b0"},
    "SecureIbex": {0: "1'b0", False: "1'b0"},
    "WritebackStage": {0: "1'b0", False: "1'b0", 1: "1'b1", True: "1'b1"},
    "BranchTargetALU": {0: "1'b0", False: "1'b0", 1: "1'b1", True: "1'b1"},
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def specialize_ibex_update(
    update: HardwareProjectUpdate,
    parameters: Mapping[str, Any],
) -> tuple[HardwareProjectUpdate, dict[str, Any]]:
    """Apply reviewed parameter defaults, failing closed on any source drift."""
    candidates = [item for item in update.files if item.path.endswith("/ibex_core.sv")
                  or item.path in {"ibex_core.sv", "rtl/ibex_core.sv"}]
    if len(candidates) != 1:
        raise IbexSpecializationError(
            f"expected exactly one ibex_core.sv, found {len(candidates)}")
    source = candidates[0]
    original = source.content
    transformed = original
    applied: list[dict[str, str]] = []
    for name, configured in parameters.items():
        if name not in _VALUE_MAP:
            raise IbexSpecializationError(f"unsupported Ibex parameter: {name}")
        try:
            replacement_value = _VALUE_MAP[name][configured]
        except (KeyError, TypeError) as exc:
            raise IbexSpecializationError(
                f"unsupported value for Ibex parameter {name}: {configured!r}") from exc
        pattern = re.compile(
            rf"(\bparameter\s+(?:bit|rv32m_e|rv32b_e|rv32zc_e)\s+{re.escape(name)}\s*=\s*)"
            rf"([^,\n]+)(\s*,?)"
        )
        matches = list(pattern.finditer(transformed))
        if len(matches) != 1:
            raise IbexSpecializationError(
                f"expected one declaration for {name}, found {len(matches)}")
        old_value = matches[0].group(2).strip()
        transformed = pattern.sub(
            lambda match: f"{match.group(1)}{replacement_value}{match.group(3)}",
            transformed,
            count=1,
        )
        applied.append({"parameter": name, "from": old_value, "to": replacement_value})

    files = [item.model_copy(update={"content": transformed}) if item is source else item
             for item in update.files]
    report = {
        "schema": "ibex-specialization/v1",
        "source_path": source.path,
        "input_sha256": _sha(original),
        "output_sha256": _sha(transformed),
        "parameters": dict(parameters),
        "transforms": applied,
        "upstream_source_preserved": True,
        "note": "Immutable source validation is retained; this is a separate execution-profile artifact.",
    }
    targets = {key: dict(value) for key, value in update.build_targets.items()}
    for target in targets.values():
        target["parameter_specialization"] = report
    provenance = dict(update.provenance)
    provenance["parameter_specialization"] = report
    return update.model_copy(update={
        "files": files,
        "build_targets": targets,
        "provenance": provenance,
    }), report


__all__ = ["IbexSpecializationError", "specialize_ibex_update"]
