import json
from pathlib import Path

from golden_models import (
    RegressionCorpus,
    RegressionStatus,
    get_reference_model,
    run_case,
    run_corpus,
    seed_case,
    seeded_corpus,
)


def test_seeded_vectors_are_deterministic_and_include_corners():
    first = seed_case("alu8", seed=42, count=12)
    second = seed_case("alu8", seed=42, count=12)
    assert first == second
    assert first.vectors[0].inputs == {"a": 0, "b": 0, "op": "add"}
    assert first.vectors[1].inputs == {"a": 255, "b": 1, "op": "add"}


def test_reference_passes_a_correct_dut_and_detects_mismatch():
    case = seed_case("arbiter4", seed=3, count=16)
    model = get_reference_model("arbiter4")
    assert run_case(case, model.evaluate).status == RegressionStatus.PASSED

    def broken(inputs):
        return {"grant": 0, "valid": 0}

    result = run_case(case, broken)
    assert result.status == RegressionStatus.DUT_FAILURE
    assert result.mismatches[0].kind == "output_mismatch"
    assert result.reference_error is None


def test_reference_and_dut_failures_are_separate():
    case = seed_case("counter8", count=4)
    stale = type(case)(case.case_id, case.model, "0.9.0", case.vectors)
    stale_result = run_case(stale, lambda _: {})
    assert stale_result.status == RegressionStatus.REFERENCE_FAILURE
    assert "reference version" in stale_result.reference_error
    model = get_reference_model("counter8")
    assert run_case(case, model.evaluate).status == RegressionStatus.PASSED

    def raises(_):
        raise RuntimeError("simulator crashed")

    result = run_case(seed_case("counter8", count=4), raises)
    assert result.status == RegressionStatus.DUT_FAILURE
    assert result.mismatches[0].kind == "dut_exception"
    assert "simulator crashed" in result.dut_error


def test_corpus_round_trip_and_discovered_bug(tmp_path):
    corpus = seeded_corpus(tmp_path / "corpus", seed=7, count=10)
    assert {case.model for case in corpus.cases()} == {"alu8", "counter8", "arbiter4"}
    bug_path = corpus.add_discovered_bug(
        model="alu8",
        inputs={"a": 255, "b": 1, "op": "add"},
        case_id="alu8-bug-carry-wrap",
        description="Preserve carry behavior found by differential testing.",
    )
    raw = json.loads(bug_path.read_text())
    assert raw["case"]["source"] == "discovered_bug"
    assert raw["reference_provenance"]["independent_of_rtl"] is True
    assert corpus.load(bug_path).vectors[0].expected["carry"] == 1


def test_changed_stored_expected_is_reference_failure(tmp_path):
    corpus = RegressionCorpus(tmp_path)
    path = corpus.save(seed_case("alu8", count=4))
    raw = json.loads(path.read_text())
    raw["case"]["vectors"][0]["expected"]["y"] = 99
    path.write_text(json.dumps(raw))
    result = run_case(corpus.load(path), lambda _: {})
    assert result.status == RegressionStatus.REFERENCE_FAILURE
    assert "stored expected output changed" in result.reference_error


def test_corpus_runner_marks_missing_adapters_inconclusive(tmp_path):
    corpus = seeded_corpus(tmp_path, count=4)
    results = run_corpus(corpus, {"alu8": get_reference_model("alu8").evaluate})
    assert results[0].status == RegressionStatus.PASSED
    assert {result.status for result in results[1:]} == {RegressionStatus.INCONCLUSIVE}


def test_checked_in_corpus_has_all_seed_blocks():
    root = Path(__file__).parents[1] / "regression_corpus"
    corpus = RegressionCorpus(root)
    assert {case.model for case in corpus.cases()} == {"alu8", "counter8", "arbiter4"}
    for case in corpus.cases():
        assert run_case(case, get_reference_model(case.model).evaluate).status == RegressionStatus.PASSED

