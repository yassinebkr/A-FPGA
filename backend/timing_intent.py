"""Validation for explicit timing-repair intent.

The model should declare what a timing retry is meant to change.  This is a
small contract used by the controller before starting another synthesis; it
does not attempt to infer RTL source locations from synthesized names (which
is inherently brittle).
"""

from __future__ import annotations

from typing import Any, Mapping

from timing_optimizer import (
    contract_preservation_errors,
    validate_strategy_for_diagnosis,
)


REQUIRED_FIELDS = ("strategy", "rationale", "expected_change")


def timing_failure(diagnosis: Mapping[str, Any] | None) -> bool:
    """Whether a diagnosis contains a measured negative setup/hold slack."""
    if not isinstance(diagnosis, Mapping):
        return False
    for key in ("setup_slack_ns", "hold_slack_ns", "wns_ns"):
        try:
            if diagnosis.get(key) is not None and float(diagnosis[key]) < 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def validate_timing_retry_intent(
    intent: Mapping[str, Any] | None,
    diagnosis: Mapping[str, Any] | None,
    *,
    require_path: bool = True,
    require_supported_strategy: bool = False,
    architecture_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate an explicit optimization intent before a timing retry.

    Required fields are deliberately semantic rather than source-specific:
    ``strategy`` (for example ``pipeline_reduction_tree``), ``rationale``,
    and ``expected_change``.  If a measured failure has a parsed critical
    path, ``target_startpoint`` and ``target_endpoint`` are required as the
    report names the model intends to address.  They are not matched against
    RTL identifiers; synthesized names often change after a harmless rewrite.
    If no path is available, ``evidence`` must state what timing report or
    metric motivated the retry.
    """
    failed = timing_failure(diagnosis)
    if not failed:
        return {"ok": True, "required": False, "missing": [],
                "reason": "no measured negative slack", "reason_code": "not_required"}
    if not isinstance(intent, Mapping):
        strategy_check = (
            validate_strategy_for_diagnosis(None, diagnosis)
            if require_supported_strategy else None
        )
        return {"ok": False, "required": True,
                "missing": list(REQUIRED_FIELDS),
                "reason": "timing retry requires explicit optimization intent",
                "classification": strategy_check.get("classification") if strategy_check else None,
                "allowed_strategies": strategy_check.get("allowed_strategies") if strategy_check else None,
                "strategy_reason": "timing_intent is required before selecting a repair strategy",
                "reason_code": "missing_intent",
                "contract_errors": []}

    missing = [field for field in REQUIRED_FIELDS
               if not isinstance(intent.get(field), str) or not intent[field].strip()]
    paths = diagnosis.get("paths") if isinstance(diagnosis, Mapping) else None
    has_path = isinstance(paths, list) and bool(paths)
    if require_path and has_path:
        for field in ("target_startpoint", "target_endpoint"):
            value = intent.get(field)
            if not isinstance(value, str) or not value.strip():
                missing.append(field)
    elif require_path and not has_path:
        evidence = intent.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            missing.append("evidence")
    strategy_check = (
        validate_strategy_for_diagnosis(intent, diagnosis)
        if require_supported_strategy else None
    )
    if strategy_check is not None and not strategy_check["ok"]:
        missing.append("supported_strategy")
    preservation_errors = contract_preservation_errors(architecture_contract, intent)
    if preservation_errors:
        missing.append("contract_preservation")
    if not missing:
        reason_code = "accepted"
    elif preservation_errors:
        reason_code = "contract_mismatch"
    elif strategy_check is not None and not strategy_check["ok"]:
        reason_code = strategy_check.get("reason_code", "unsupported_strategy")
    else:
        reason_code = "missing_intent_fields"
    return {
        "ok": not missing,
        "required": True,
        "missing": missing,
        "reason": ("intent accepted" if not missing else
                   "timing retry intent is incomplete"),
        "reason_code": reason_code,
        "classification": strategy_check.get("classification") if strategy_check else None,
        "allowed_strategies": strategy_check.get("allowed_strategies") if strategy_check else None,
        "strategy_reason": strategy_check.get("reason") if strategy_check else None,
        "contract_errors": preservation_errors,
    }
