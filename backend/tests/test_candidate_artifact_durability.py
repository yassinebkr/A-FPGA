"""Candidate checkpoints must be backed by reloadable content-addressed bytes."""

from pathlib import Path

import agent_core
from evidence import EvidenceRepository


def _snapshot(code: str, testbench: str) -> dict:
    return {
        "candidate_id": "cand-fixture",
        "code_sha256": agent_core._code_sha256(code),
        "verilog_code": code,
        "testbench_code": testbench,
        "testbench_sha256": agent_core._code_sha256(testbench),
        "functional_passed": True,
    }


def test_lineage_migrates_inline_sources_to_content_addressed_store(
        tmp_path, monkeypatch):
    repository = EvidenceRepository(tmp_path / "evidence")
    monkeypatch.setattr(agent_core, "_evidence_repository", repository)
    value = _snapshot("module m; endmodule\n", "module tb; endmodule\n")

    clean = agent_core._lineage_snapshot(value)

    assert clean is not None
    assert repository.get_artifact(clean["rtl_artifact_sha256"]) == value["verilog_code"].encode()
    assert repository.get_artifact(clean["testbench_artifact_sha256"]) == value["testbench_code"].encode()


def test_missing_referenced_rtl_fails_closed_without_inline_fallback(
        tmp_path, monkeypatch):
    repository = EvidenceRepository(tmp_path / "evidence")
    monkeypatch.setattr(agent_core, "_evidence_repository", repository)
    value = _snapshot("module m; endmodule\n", "module tb; endmodule\n")
    value["rtl_artifact_sha256"] = "0" * 64

    assert agent_core._lineage_snapshot(value) is None


def test_missing_referenced_testbench_invalidates_functional_checkpoint(
        tmp_path, monkeypatch):
    repository = EvidenceRepository(tmp_path / "evidence")
    monkeypatch.setattr(agent_core, "_evidence_repository", repository)
    value = _snapshot("module m; endmodule\n", "module tb; endmodule\n")
    value["rtl_artifact_sha256"] = repository.put_artifact(
        value["verilog_code"].encode(), name="m.sv").sha256
    value["testbench_artifact_sha256"] = "f" * 64

    assert agent_core._lineage_snapshot(value) is None


def test_artifact_bytes_survive_project_restart(tmp_path, monkeypatch):
    repository = EvidenceRepository(tmp_path / "evidence")
    monkeypatch.setattr(agent_core, "_evidence_repository", repository)
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(agent_core, "_project_repository", None)
    sid = "proj-durable-candidate"
    agent_core.reset_session(sid)
    agent_core._get_history(sid)
    state = agent_core._get_state(sid)
    code = "module m; endmodule\n"
    testbench = "module tb; endmodule\n"
    snapshot = agent_core._lineage_snapshot(_snapshot(code, testbench))
    state["revisions"] = [snapshot]
    state["revision_by_hash"] = {snapshot["code_sha256"]: snapshot}
    state["best_revision"] = snapshot
    state["active_revision"] = snapshot
    agent_core._save_project(sid)

    agent_core._session_state.pop(sid, None)
    agent_core._histories.pop(sid, None)
    agent_core._get_history(sid)

    restored = agent_core._get_state(sid)["active_revision"]
    assert restored["verilog_code"] == code
    assert restored["testbench_code"] == testbench
    assert repository.get_artifact(restored["rtl_artifact_sha256"]) == code.encode()
    agent_core.reset_session(sid)


def test_simulation_pass_is_rejected_when_artifact_write_fails(monkeypatch):
    sid = "artifact-write-failure"
    agent_core.reset_session(sid)


def test_linted_revision_is_persisted_before_simulation(tmp_path, monkeypatch):
    repository = EvidenceRepository(tmp_path / "evidence")
    monkeypatch.setattr(agent_core, "_evidence_repository", repository)
    sid = "lint-artifact"
    agent_core.reset_session(sid)
    code = "module m; endmodule\n"

    result = agent_core._postprocess_tool_result(
        sid, "write_and_check_syntax",
        {"verilog_code": code, "top_module": "m"},
        '{"ok":true,"verdict":"ok"}')

    digest = agent_core._code_sha256(code)
    assert f"rtl_artifact_sha256: {digest}" in result
    assert repository.get_artifact(digest) == code.encode()
    agent_core.reset_session(sid)
    monkeypatch.setattr(
        agent_core, "_persist_candidate_source",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))

    result = agent_core._postprocess_tool_result(
        sid, "run_simulation",
        {"verilog_code": "module m; endmodule\n",
         "testbench_code": "module tb; endmodule\n", "top_module": "m"},
        "SIMULATION PASSED (fixture)")

    assert "CHECKPOINT REJECTED" in result
    assert agent_core._get_state(sid)["revisions"] == []
    assert agent_core._get_state(sid)["run_stop_reason"] == "artifact_persistence_failed"
    agent_core.reset_session(sid)
