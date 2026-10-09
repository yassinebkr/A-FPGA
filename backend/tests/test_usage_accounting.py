from types import SimpleNamespace

import agent_core
from usage_accounting import UsageLedger, normalize_provider_usage


def test_reasoning_details_are_output_metadata_not_extra_tokens():
    usage = {"prompt_tokens": 10, "completion_tokens": 20,
             "total_tokens": 30, "completion_tokens_details": {"reasoning_tokens": 15}}
    ledger = UsageLedger()
    result = ledger.record(usage, generation_id="g1", run_id="r1", event_id="frame-1")
    assert result["authority"] == "exact"
    assert result["reasoning_accounting"] == "output_subset"
    assert ledger.snapshot()["project"]["total_tokens"] == 30


def test_duplicate_replay_does_not_add_spend_and_scopes_are_separate():
    ledger = UsageLedger()
    usage = SimpleNamespace(prompt_tokens=4, completion_tokens=6, total_tokens=10)
    assert ledger.record(usage, generation_id="g1", run_id="r1", event_id="u1")["accepted"]
    duplicate = ledger.record(usage, generation_id="g1", run_id="r1", event_id="u1")
    assert duplicate["duplicate"] is True
    snapshot = ledger.snapshot()
    assert snapshot["project"] == {"input_tokens": 4, "output_tokens": 6, "total_tokens": 10}
    assert snapshot["runs"]["r1"]["total_tokens"] == 10


def test_missing_total_is_derived_and_estimates_are_not_provider_usage():
    ledger = UsageLedger()
    result = ledger.record({"prompt_tokens": 3, "completion_tokens": 2}, event_id="u2")
    assert result["authority"] == "derived"
    ledger.record_prompt_estimate("g2", 999)
    assert ledger.snapshot()["project"]["total_tokens"] == 5
    assert ledger.snapshot()["prompt_estimates"] == {"g2": 999}


def test_snapshot_round_trip_preserves_dedupe_and_totals():
    ledger = UsageLedger()
    ledger.record({"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, event_id="u3")
    restored = UsageLedger.from_snapshot(ledger.snapshot())
    duplicate = restored.record({"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, event_id="u3")
    assert duplicate["duplicate"] is True
    assert restored.snapshot()["project"]["total_tokens"] == 3


def test_normalization_accepts_openai_aliases_and_unknown_is_explicit():
    assert normalize_provider_usage({"input_tokens": 2, "output_tokens": 3})["total_tokens"] == 5
    assert normalize_provider_usage({}) == {}
    assert UsageLedger().record(None)["authority"] == "unknown"


def test_reasoning_models_get_reasoning_sized_output_ceiling(monkeypatch):
    """Reasoning models (Kimi K3, Mistral GLM 5.3) bill thinking against the
    completion budget, so their per-request ceiling must be larger than the
    plain-model cap — otherwise thinking alone hits finish_reason=length."""
    sid = "s5-reasoning-ceiling"
    agent_core.reset_session(sid)
    monkeypatch.setattr(agent_core, "MODEL_OUTPUT_TOKEN_LIMIT", 100)
    monkeypatch.setattr(agent_core, "REASONING_OUTPUT_TOKEN_LIMIT", 200)
    monkeypatch.setattr(agent_core, "CHAT_TOKEN_BUDGET", 10_000)
    monkeypatch.setattr(agent_core, "CONTEXT_HARD_LIMIT", 100_000)
    monkeypatch.setattr(agent_core, "_session_model_reasoning", lambda _: True)
    result = agent_core._context_preflight(
        sid, [{"role": "user", "content": "small"}], [])
    assert result["output_cap"] == 200
    assert result["output_allowance"] == 200


def test_plain_models_keep_the_plain_output_ceiling(monkeypatch):
    sid = "s5-plain-ceiling"
    agent_core.reset_session(sid)
    monkeypatch.setattr(agent_core, "MODEL_OUTPUT_TOKEN_LIMIT", 100)
    monkeypatch.setattr(agent_core, "REASONING_OUTPUT_TOKEN_LIMIT", 200)
    monkeypatch.setattr(agent_core, "CHAT_TOKEN_BUDGET", 10_000)
    monkeypatch.setattr(agent_core, "CONTEXT_HARD_LIMIT", 100_000)
    monkeypatch.setattr(agent_core, "_session_model_reasoning", lambda _: False)
    result = agent_core._context_preflight(
        sid, [{"role": "user", "content": "small"}], [])
    assert result["output_cap"] == 100
    assert result["output_allowance"] == 100


def test_context_preflight_reserves_output_and_run_allowance(monkeypatch):
    sid = "s5-output-admission"
    agent_core.reset_session(sid)
    monkeypatch.setattr(agent_core, "MODEL_OUTPUT_TOKEN_LIMIT", 100)
    monkeypatch.setattr(agent_core, "CHAT_TOKEN_BUDGET", 150)
    monkeypatch.setattr(agent_core, "CONTEXT_HARD_LIMIT", 10_000)
    state = agent_core._get_state(sid)
    state["tokens_used"] = 120
    state["chat_token_start"] = 0
    result = agent_core._context_preflight(
        sid, [{"role": "user", "content": "small"}], [])
    assert result["output_allowance"] == 30 - result["estimate"]
    assert result["envelope"] == 30
    assert result["run_remaining"] == 30
