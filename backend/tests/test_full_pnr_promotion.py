"""Full-P&R promotion durability and actionable admission; no EDA calls.

The one allowed full-P&R experiment is bound to an exact promoted candidate
and its promotion token. Two failure modes are covered here: the promotion
used to vanish on a backend restart (it was never serialized with the
candidate lineage), and a model that regenerates the RTL instead of
re-referencing the promoted source used to get the generic
simulation-checkpoint rejection instead of a corrective instruction.
"""

import agent_core
from architecture_contract import define_architecture


def _payload(**changes):
    value = {
        "target_type": "asic",
        "top_module": "dut",
        "technology": "sky130",
        "clock_target_mhz": 50,
        "clock_period_ns": 20,
        "latency_cycles": 2,
        "throughput_per_cycle": 1,
        "pipeline_stages": 1,
        "multiplier_strategy": "inferred",
        "memory_strategy": "registers",
    }
    value.update(changes)
    return value


RTL = "module dut(input clk); always @(posedge clk) begin end endmodule"
OTHER_RTL = "module dut(input clk, input rst); always @(posedge clk) begin end endmodule"


def _approved_session(sid):
    """A session with a locked contract and a pending promoted candidate."""
    agent_core.reset_session(sid)
    agent_core._begin_chat_run(sid, require_architecture=True)
    proposal = define_architecture(**_payload())
    agent_core._postprocess_tool_result(sid, "define_architecture", _payload(), proposal)
    surface = agent_core.get_design_contract(sid)
    agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=surface["revision"], expected_digest=surface["digest"])
    agent_core._begin_chat_run(sid, require_architecture=True, resume_contract=True)
    st = agent_core._get_state(sid)
    digest = agent_core._code_sha256(RTL)
    revision = {
        "candidate_id": f"cand-{digest[:16]}", "revision": 1,
        "code_sha256": digest, "verilog_code": RTL, "top_module": "dut",
        "architecture_contract": st.get("architecture_contract") or {},
        "architecture_contract_hash": st.get("architecture_contract_hash"),
        "testbench_code": "module tb; endmodule",
        "testbench_sha256": agent_core._code_sha256("module tb; endmodule"),
        "functional_passed": True, "synthesis_passed": True,
        "metrics": {"setupslack": 0.5}, "status": "promoted",
        "promotion_token": "tok-abc123",
        "screen_evidence_sha256": "evidence-1",
    }
    # The binding is computed exactly like the admission gate does: with the
    # candidate itself as the checkpoint context (it carries the testbench).
    revision["promotion_binding"] = agent_core._promotion_binding(
        {"verilog_code": RTL, "top_module": "dut", "clock_period_ns": 20}, revision)
    st["revision_by_hash"][digest] = revision
    st["revisions"].append(dict(revision))
    st["promoted_revision"] = dict(revision)
    st["promotion_baseline"] = dict(revision)
    st["last_good_verilog"] = RTL
    return st, revision


def test_promotion_survives_serialize_validate_restore():
    """The promoted revision (with its token) round-trips through the
    candidate lineage, so a backend restart keeps the one allowed full-P&R
    experiment available instead of orphaning its token."""
    st, revision = _approved_session("pnr-restore")
    try:
        serialized = agent_core._serialize_candidate_lineage(st)
        assert serialized is not None
        assert serialized["promoted"]["code_sha256"] == revision["code_sha256"]
        assert serialized["promoted"]["promotion_token"] == "tok-abc123"
        validated = agent_core._validated_candidate_lineage(serialized)
        assert validated is not None and validated["promoted"] is not None
        assert validated["promoted"]["promotion_token"] == "tok-abc123"
        # A fresh process state (backend restart) restores the promotion.
        agent_core.reset_session("pnr-restore-2")
        fresh = agent_core._get_state("pnr-restore-2")
        agent_core._restore_candidate_lineage(fresh, validated)
        assert fresh["promoted_revision"]["code_sha256"] == revision["code_sha256"]
        assert fresh["promoted_revision"]["promotion_token"] == "tok-abc123"
        assert fresh["promotion_baseline"]["code_sha256"] == revision["code_sha256"]
    finally:
        agent_core.reset_session("pnr-restore")
        agent_core.reset_session("pnr-restore-2")


def test_full_pnr_with_wrong_source_gets_the_corrective_instruction():
    """A regenerated RTL with a valid token is a different, unverified
    candidate: the rejection names the promoted checkpoint and its artifact
    handle so the model can correct itself in the next round."""
    _approved_session("pnr-mismatch")
    try:
        blocked = agent_core._before_tool_call("pnr-mismatch", "run_silicon_compiler", {
            "verilog_code": OTHER_RTL, "top_module": "dut",
            "clock_period_ns": 20, "flow": "full-pnr",
            "promotion_token": "tok-abc123"})
        assert blocked and "FULL PNR SOURCE MISMATCH" in blocked
        digest16 = agent_core._code_sha256(RTL)[:16]
        assert f"[artifact rtl:{digest16}]" in blocked
        assert "never regenerate" in blocked.lower() or "do not regenerate" in blocked.lower()
    finally:
        agent_core.reset_session("pnr-mismatch")


def test_full_pnr_with_matching_token_and_source_is_admitted():
    st, revision = _approved_session("pnr-admitted")
    try:
        assert agent_core._before_tool_call("pnr-admitted", "run_silicon_compiler", {
            "verilog_code": RTL, "top_module": "dut",
            "clock_period_ns": 20, "flow": "full-pnr",
            "promotion_token": "tok-abc123"}) is None
    finally:
        agent_core.reset_session("pnr-admitted")


def test_full_pnr_with_unknown_token_gets_the_reearn_instruction():
    """An orphaned token (promotion lost to a restart) plus an unverified
    source must produce the corrective re-earn path, never the generic
    simulation-checkpoint message the model cannot act on."""
    _approved_session("pnr-unknown-token")
    try:
        # Simulate the restart: the promotion is gone from memory.
        st = agent_core._get_state("pnr-unknown-token")
        st["promoted_revision"] = None
        blocked = agent_core._before_tool_call("pnr-unknown-token", "run_silicon_compiler", {
            "verilog_code": OTHER_RTL, "top_module": "dut",
            "clock_period_ns": 20, "flow": "full-pnr",
            "promotion_token": "tok-from-a-previous-process"})
        assert blocked and "FULL PNR TOKEN UNKNOWN" in blocked
        assert "screening synthesis" in blocked
        assert "CURRENT ARTIFACTS" in blocked
        assert "never a" in blocked and "regenerated RTL" in blocked
    finally:
        agent_core.reset_session("pnr-unknown-token")


def test_durable_sim_checkpoint_seeds_last_good_verilog_on_load(tmp_path, monkeypatch):
    """A project whose lineage has no active member (staged screen) still
    restores the verified source from the durable SIMULATION PASSED
    transcript, so CURRENT ARTIFACTS offers the sim-passed RTL to the model
    instead of a later unverified rewrite."""
    import json
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    sid = "proj-durable-sim"
    agent_core._histories[sid] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "build"},
        {"role": "assistant", "tool_calls": [
            {"id": "s1", "type": "function",
             "function": {"name": "run_simulation",
                          "arguments": json.dumps({"verilog_code": RTL,
                                                   "testbench_code": "module tb; endmodule",
                                                   "top_module": "dut"})}}]},
        {"role": "tool", "tool_call_id": "s1", "content": "SIMULATION PASSED"},
        # A later rewrite that never passed simulation.
        {"role": "assistant", "tool_calls": [
            {"id": "w1", "type": "function",
             "function": {"name": "write_and_check_syntax",
                          "arguments": json.dumps({"verilog_code": OTHER_RTL,
                                                   "top_module": "dut"})}}]},
        {"role": "tool", "tool_call_id": "w1", "content": '{"ok": true}'},
    ]
    agent_core._project_names[sid] = "Durable sim"
    agent_core._save_project(sid)
    try:
        # Simulate a backend restart: fresh in-memory state, load from disk.
        agent_core._session_state.pop(sid, None)
        agent_core._histories.pop(sid, None)
        agent_core._load_project(sid)
        state = agent_core._get_state(sid)
        assert state.get("last_good_verilog") == RTL
    finally:
        agent_core.reset_session(sid)


def test_corrective_full_pnr_gates_do_not_stop_the_run():
    """The TOKEN UNKNOWN / SOURCE MISMATCH results are corrective instructions
    for the model: they must NOT set run_stop_reason, or the loop would
    terminate before the model can re-screen in the same run (the 18:52
    field failure — the run died right after the instruction)."""
    _approved_session("pnr-nonterminal")
    try:
        st = agent_core._get_state("pnr-nonterminal")
        st["promoted_revision"] = None
        blocked = agent_core._before_tool_call("pnr-nonterminal", "run_silicon_compiler", {
            "verilog_code": RTL, "top_module": "dut",
            "clock_period_ns": 20, "flow": "full-pnr",
            "promotion_token": "tok-gone"})
        assert blocked and "FULL PNR TOKEN UNKNOWN" in blocked
        assert st.get("run_stop_reason") is None

        # Restore the promotion and force a source mismatch: same property.
        st["promoted_revision"] = dict(st["revision_by_hash"][agent_core._code_sha256(RTL)])
        mismatch = agent_core._before_tool_call("pnr-nonterminal", "run_silicon_compiler", {
            "verilog_code": OTHER_RTL, "top_module": "dut",
            "clock_period_ns": 20, "flow": "full-pnr",
            "promotion_token": "tok-abc123"})
        assert mismatch and "FULL PNR SOURCE MISMATCH" in mismatch
        assert st.get("run_stop_reason") is None
    finally:
        agent_core.reset_session("pnr-nonterminal")
