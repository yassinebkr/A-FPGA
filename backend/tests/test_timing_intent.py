from timing_intent import validate_timing_retry_intent


def test_positive_timing_does_not_require_intent():
    result = validate_timing_retry_intent(None, {"setup_slack_ns": 0.1})
    assert result["ok"] is True
    assert result["required"] is False


def test_negative_timing_requires_semantic_intent_and_report_path():
    diagnosis = {
        "setup_slack_ns": -2.43,
        "paths": [{"startpoint": "u_prod/Q", "endpoint": "u_acc/D"}],
    }
    missing = validate_timing_retry_intent(None, diagnosis)
    assert missing["ok"] is False
    assert set(missing["missing"]) == {
        "strategy", "rationale", "expected_change",
    }

    accepted = validate_timing_retry_intent({
        "strategy": "pipeline_accumulation_tree",
        "rationale": "The reported path crosses the unregistered reduction tree.",
        "expected_change": "Add one register stage before the accumulator.",
        "target_startpoint": "u_prod/Q",
        "target_endpoint": "u_acc/D",
    }, diagnosis)
    assert accepted["ok"] is True


def test_negative_timing_without_path_requires_evidence_text():
    diagnosis = {"setup_slack_ns": -0.2, "paths": []}
    result = validate_timing_retry_intent({
        "strategy": "reduce_fanout",
        "rationale": "The slack is negative at the requested period.",
        "expected_change": "Register the high-fanout enable.",
    }, diagnosis)
    assert result["ok"] is False
    assert result["missing"] == ["evidence"]

