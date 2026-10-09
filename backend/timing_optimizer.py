"""Deterministic critical-path classification and timing-repair policy.

This module intentionally operates on normalized timing evidence and a small
semantic retry intent.  It does not inspect RTL or claim that a proposed
rewrite will close timing; the EDA measurement remains authoritative.  Its
job is to make the next experiment explainable and bounded before any EDA
process is started.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


CATEGORIES = (
    "multiplier_to_accumulator",
    "wide_adder_carry_chain",
    "mux_decode",
    "fanout",
    "memory_read",
    "control",
    "unknown",
)


# Ordered rules matter: a MAC path should be classified as multiplier-to-
# accumulator before the broader adder/control rules see "acc" or "enable".
_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("multiplier_to_accumulator", (
        r"\bmul(?:tiplier)?\b", r"\bproduct\b", r"\bmac\b",
        r"\bacc(?:umulator)?\b.*\b(?:mul|prod)\b",
        r"\b(?:mul|prod)\b.*\bacc(?:umulator)?\b",
    )),
    ("wide_adder_carry_chain", (
        r"\b(?:adder|add|sum|reduce|reduction|carry|acc(?:umulator)?)\b",
        r"\b(?:carry_chain|ripple|cla)\b",
    )),
    ("mux_decode", (
        r"\bmux(?:es)?\b", r"\b(?:select|selector|decode|decoder|onehot|one_hot)\b",
    )),
    ("fanout", (
        r"\bfanout\b", r"\bhigh[_ -]?fanout\b", r"\b(?:driver|replica)\b",
    )),
    ("memory_read", (
        r"\b(?:mem|memory|ram|sram|bram|rom)\b", r"\b(?:read|rd)_?(?:data|addr|port)\b",
    )),
    ("control", (
        r"\b(?:fsm|state|control|enable|valid|ready|stall|arbiter|handshake)\b",
    )),
)


# These are semantic names accepted in timing_intent.strategy. Bare generic
# transforms are intentionally absent: every retry must select a category-specific
# transformation supported by the measured path.
STRATEGIES: dict[str, tuple[str, ...]] = {
    "multiplier_to_accumulator": (
        "pipeline_multiplier", "register_product", "pipeline_accumulator",
        "balanced_accumulation_tree", "pipeline_accumulation_tree",
    ),
    "wide_adder_carry_chain": (
        "balanced_reduction_tree", "pipeline_adder_tree", "pipeline_accumulation_tree",
        "shorten_carry_chain",
    ),
    "mux_decode": (
        "reduce_mux_depth", "register_mux_output", "simplify_decode",
    ),
    "fanout": (
        "reduce_fanout", "duplicate_drivers", "register_fanout",
    ),
    "memory_read": (
        "pipeline_memory_read", "register_memory_read", "infer_bram",
    ),
    "control": (
        "pipeline_control", "simplify_control", "register_control",
    ),
    "unknown": (),
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return " ".join(f"{key} {item}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_text(item) for item in value)
    return str(value)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _path_text(path: Mapping[str, Any]) -> str:
    return " ".join(
        _text(path.get(key)) for key in (
            "path", "startpoint", "endpoint", "path_group", "clock", "logic",
            "cells", "description", "critical_path",
        )
    ).strip()


def _match_text(text: str) -> str:
    # Synthesized identifiers commonly use underscores, slashes, dots and
    # packed-index brackets. Treat those as token separators for classification
    # while retaining the original evidence in the returned diagnosis.
    return re.sub(r"[_./\[\]():$-]+", " ", text).lower()


def _best_path(diagnosis: Mapping[str, Any]) -> Mapping[str, Any] | None:
    paths = diagnosis.get("paths")
    if not isinstance(paths, list) or not paths:
        return None
    mappings = [path for path in paths if isinstance(path, Mapping)]
    if not mappings:
        return None
    # Prefer the worst measured setup path; deterministic source order breaks
    # ties and is more useful than picking an arbitrary report path.
    return min(
        enumerate(mappings),
        key=lambda item: (
            _number(item[1].get("slack_ns"))
            if _number(item[1].get("slack_ns")) is not None else float("inf"),
            item[0],
        ),
    )[1]


def classify_timing_path(diagnosis: Mapping[str, Any] | None) -> dict[str, Any]:
    """Classify the measured critical path into a stable structural category."""
    data = diagnosis if isinstance(diagnosis, Mapping) else {}
    path = _best_path(data)
    if path is None:
        raw = data.get("critical_path")
        path = {"path": raw} if raw else {}
    raw_text = _path_text(path)
    text = _match_text(raw_text)
    category = "unknown"
    matched: list[str] = []
    for candidate, patterns in _CATEGORY_RULES:
        hits = [pattern for pattern in patterns if re.search(pattern, text, re.IGNORECASE)]
        if hits:
            category = candidate
            matched = hits
            break
    strategies = list(STRATEGIES[category])
    endpoint = _text(path.get("endpoint")) or None
    startpoint = _text(path.get("startpoint")) or None
    slack = _number(path.get("slack_ns"))
    if slack is None:
        slack = _number(data.get("setup_slack_ns"))
    evidence = raw_text or "No parsed critical-path names were available."
    label = category.replace("_", " ")
    return {
        "category": category,
        "label": label,
        "confidence": "high" if matched else "low",
        "matched_terms": matched,
        "startpoint": startpoint,
        "endpoint": endpoint,
        "slack_ns": slack,
        "evidence": evidence,
        "recommended_strategies": strategies,
    }


def strategy_allowed(strategy: Any, classification: Mapping[str, Any] | None) -> bool:
    """Return whether an intent strategy is valid for the measured category."""
    if not isinstance(strategy, str) or not strategy.strip():
        return False
    category = (classification or {}).get("category", "unknown")
    return strategy.strip().lower() in STRATEGIES.get(category, STRATEGIES["unknown"])


def validate_strategy_for_diagnosis(
    intent: Mapping[str, Any] | None,
    diagnosis: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate that an explicit strategy is evidence-supported."""
    classification = classify_timing_path(diagnosis)
    strategy = intent.get("strategy") if isinstance(intent, Mapping) else None
    normalized = strategy.strip().lower() if isinstance(strategy, str) else ""
    allowed = strategy_allowed(normalized, classification)
    path_target_ok = True
    measured_start = classification.get("startpoint")
    measured_end = classification.get("endpoint")
    if measured_start or measured_end:
        requested_start = _text(intent.get("target_startpoint")) if isinstance(intent, Mapping) else ""
        requested_end = _text(intent.get("target_endpoint")) if isinstance(intent, Mapping) else ""
        path_target_ok = (requested_start == measured_start and requested_end == measured_end)
        if not path_target_ok:
            allowed = False
    if allowed:
        reason_code = "accepted"
    elif not path_target_ok:
        reason_code = "endpoint_mismatch"
    else:
        reason_code = "unsupported_strategy"
    return {
        "ok": allowed,
        "reason_code": reason_code,
        "strategy": normalized or None,
        "category": classification["category"],
        "classification": classification,
        "allowed_strategies": classification["recommended_strategies"],
        "path_target_ok": path_target_ok,
        "reason": (
            "strategy and path targets are supported by the measured critical-path category"
            if allowed else
            ("timing_intent targets do not match the measured critical path"
             if not path_target_ok else
             f"strategy {normalized or '<missing>'} is not supported for "
             f"{classification['category']}; choose one of "
             f"{', '.join(classification['recommended_strategies'])}")
        ),
    }


def contract_preservation_errors(
    contract: Mapping[str, Any] | None,
    intent: Mapping[str, Any] | None,
) -> list[str]:
    """Reject timing intents that silently change latency/throughput targets."""
    if not isinstance(contract, Mapping) or not isinstance(intent, Mapping):
        return []
    errors: list[str] = []
    for field, label in (("latency_cycles", "latency"),
                         ("throughput_per_cycle", "throughput")):
        if field not in intent:
            continue
        try:
            expected = float(contract[field])
            actual = float(intent[field])
        except (KeyError, TypeError, ValueError):
            errors.append(f"timing_intent {label} must preserve the architecture contract")
            continue
        if actual != expected:
            errors.append(
                f"timing_intent {label} {actual:g} contradicts contract {expected:g}"
            )
    return errors


__all__ = [
    "CATEGORIES", "STRATEGIES", "classify_timing_path", "strategy_allowed",
    "validate_strategy_for_diagnosis", "contract_preservation_errors",
]
