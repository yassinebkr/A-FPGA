"""Provider usage accounting with explicit authority and replay semantics.

This module deliberately does not estimate provider usage.  A provider supplied
``total_tokens`` is authoritative; prompt estimates are recorded separately by
the caller and never added to the paid total.  Reasoning tokens are metadata
and, for the OpenAI-compatible APIs used here, normally a subset of output
tokens, so they are never added a second time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def _value(source: Any, *names: str) -> Any:
    for name in names:
        value = source.get(name) if isinstance(source, Mapping) else getattr(source, name, None)
        if value is not None:
            return value
    return None


def _nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def normalize_provider_usage(usage: Any) -> dict[str, int]:
    """Normalize common OpenAI-compatible usage shapes.

    ``total_tokens`` is retained when supplied.  If absent, it is derived only
    from the two provider counters, and the caller can mark that result as
    derived rather than presenting it as an exact provider total.
    """
    if usage is None:
        return {}
    details = _value(usage, "completion_tokens_details", "output_tokens_details")
    reasoning = _value(usage, "reasoning_tokens")
    if reasoning is None and details is not None:
        reasoning = _value(details, "reasoning_tokens")
    result: dict[str, int] = {}
    for key, names in {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }.items():
        number = _nonnegative_int(_value(usage, *names))
        if number is not None:
            result[key] = number
    number = _nonnegative_int(reasoning)
    if number is not None:
        result["reasoning_tokens"] = number
    if "total_tokens" not in result and {"input_tokens", "output_tokens"}.issubset(result):
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    remaining = _nonnegative_int(_value(usage, "tokens_remaining"))
    if remaining is not None:
        result["tokens_remaining"] = remaining
    return result


@dataclass
class UsageLedger:
    """Serializable generation/run/project usage ledger.

    A generation may produce multiple transport frames (or be replayed after a
    reconnect).  ``event_id`` is the idempotency key; repeated records do not
    increase totals.  ``generation_id`` is retained for audit and is not itself
    treated as a dedupe key because a provider can legitimately emit distinct
    usage records within a generation.
    """

    generations: dict[str, dict[str, Any]] = field(default_factory=dict)
    seen_events: set[str] = field(default_factory=set)
    runs: dict[str, dict[str, int]] = field(default_factory=dict)
    project_totals: dict[str, int] = field(default_factory=lambda: {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
    })
    prompt_estimates: dict[str, int] = field(default_factory=dict)
    last_tokens_remaining: int | None = None

    def record(self, usage: Any, *, generation_id: str | None = None,
               run_id: str | None = None, event_id: str | None = None,
               source: str = "provider") -> dict[str, Any]:
        normalized = normalize_provider_usage(usage)
        if not normalized:
            return {"accepted": False, "authority": "unknown", "usage": {}}
        # One final provider usage object represents one generation. Replayed
        # frames with that generation id must not be charged again. Callers
        # with multiple usage records in one generation should provide an
        # explicit event_id.
        key = event_id or (f"generation:{generation_id}" if generation_id
                           else f"anonymous:{len(self.seen_events)}")
        if key in self.seen_events:
            return {"accepted": False, "duplicate": True,
                    "authority": "exact" if "total_tokens" in normalized else "derived",
                    "usage": normalized}
        self.seen_events.add(key)
        exact_total = "total_tokens" in normalize_provider_usage(usage) and _value(usage, "total_tokens") is not None
        authority = "exact" if exact_total else ("derived" if "total_tokens" in normalized else "unknown")
        record = {**normalized, "authority": authority, "source": source,
                  "run_id": run_id, "generation_id": generation_id,
                  "event_id": key}
        if "reasoning_tokens" in normalized:
            output = normalized.get("output_tokens")
            record["reasoning_accounting"] = (
                "output_subset" if output is not None and normalized["reasoning_tokens"] <= output
                else "metadata_only")
        self.generations[key] = record
        for field_name in ("input_tokens", "output_tokens", "total_tokens"):
            self.project_totals[field_name] += normalized.get(field_name, 0)
        if run_id:
            run = self.runs.setdefault(run_id, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
            for field_name in run:
                run[field_name] += normalized.get(field_name, 0)
        if "tokens_remaining" in normalized:
            self.last_tokens_remaining = normalized["tokens_remaining"]
        return {"accepted": True, "authority": authority, "usage": normalized,
                "reasoning_accounting": record.get("reasoning_accounting")}

    def record_prompt_estimate(self, generation_id: str, tokens: int) -> None:
        self.prompt_estimates[generation_id] = max(0, int(tokens))

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "project": dict(self.project_totals),
            "runs": {key: dict(value) for key, value in self.runs.items()},
            "generations": [dict(value) for value in self.generations.values()],
            "seen_events": sorted(self.seen_events),
            "prompt_estimates": dict(self.prompt_estimates),
            "tokens_remaining": self.last_tokens_remaining,
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any] | None) -> "UsageLedger":
        ledger = cls()
        if not isinstance(snapshot, Mapping) or snapshot.get("version") != 1:
            return ledger
        ledger.seen_events = {str(item) for item in snapshot.get("seen_events", [])}
        ledger.prompt_estimates = {str(k): max(0, int(v)) for k, v in (snapshot.get("prompt_estimates") or {}).items()}
        ledger.last_tokens_remaining = _nonnegative_int(snapshot.get("tokens_remaining"))
        for item in snapshot.get("generations", []):
            if isinstance(item, Mapping) and item.get("event_id"):
                ledger.generations[str(item["event_id"])] = dict(item)
        for target, source in ((ledger.project_totals, snapshot.get("project")),):
            if isinstance(source, Mapping):
                for key in target:
                    target[key] = max(0, int(source.get(key, 0)))
        for run_id, values in (snapshot.get("runs") or {}).items():
            if isinstance(values, Mapping):
                ledger.runs[str(run_id)] = {key: max(0, int(values.get(key, 0))) for key in ledger.project_totals}
        return ledger
