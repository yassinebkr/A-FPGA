"""Tests for the repair policy + token accounting enforced in the agent_core
tool loop, and for cache integration in the eda_tools entry points."""
import asyncio
import json
from types import SimpleNamespace

import agent_core
import eda_tools
import lint


def _lint_json(ok, verdict="syntax_error"):
    return json.dumps({"tool": "verilator", "ok": ok, "verdict": verdict,
                       "errors": [], "errors_total": 0 if ok else 1,
                       "warnings_total": 0, "truncated": False})


def test_syntax_budget_exhaustion():
    sid = "test-syntax-budget"
    agent_core.reset_session(sid)
    args = {"verilog_code": "module m; endmodule", "top_module": "m"}
    for i in range(1, agent_core.SYNTAX_REPAIR_CAP):
        out = agent_core._postprocess_tool_result(sid, "write_and_check_syntax",
                                                  args, _lint_json(False))
        assert "BUDGET EXHAUSTED" not in out, f"cap hit early at attempt {i}"
    # Cap hit: notice + diff of attempted changes appended.
    args2 = {"verilog_code": "module m; endmodulex", "top_module": "m"}
    out = agent_core._postprocess_tool_result(sid, "write_and_check_syntax",
                                              args2, _lint_json(False))
    assert "SYNTAX REPAIR BUDGET EXHAUSTED" in out
    assert "change strategy" in out
    assert "--- previous_attempt" in out and "+++ current_attempt" in out
    st = agent_core._get_state(sid)
    assert st["syntax_attempts"] == agent_core.SYNTAX_REPAIR_CAP
    assert st["repair_attempts"] == agent_core.SYNTAX_REPAIR_CAP
    agent_core.reset_session(sid)


def test_passing_and_non_json_results_dont_count():
    sid = "test-no-count"
    agent_core.reset_session(sid)
    args = {"verilog_code": "module m; endmodule", "top_module": "m"}
    agent_core._postprocess_tool_result(sid, "write_and_check_syntax", args, _lint_json(True, "ok"))
    agent_core._postprocess_tool_result(sid, "write_and_check_syntax", args,
                                        _lint_json(True, "lint_warning"))
    agent_core._postprocess_tool_result(sid, "write_and_check_syntax", args,
                                        "Code saved to x, but no HDL linter is installed")
    st = agent_core._get_state(sid)
    assert st["syntax_attempts"] == 0 and st["repair_attempts"] == 0
    agent_core.reset_session(sid)


def test_sim_pass_resets_syntax_budget_and_snapshots():
    sid = "test-sim-pass"
    agent_core.reset_session(sid)
    lint_args = {"verilog_code": "module m; endmodule", "top_module": "m"}
    for _ in range(agent_core.SYNTAX_REPAIR_CAP - 1):
        agent_core._postprocess_tool_result(sid, "write_and_check_syntax",
                                            lint_args, _lint_json(False))
    sim_args = {"verilog_code": "module m; endmodule", "testbench_code": "tb",
                "top_module": "m"}
    agent_core._postprocess_tool_result(sid, "run_simulation", sim_args,
                                        "SIMULATION PASSED (iverilog):\nPASS\njob_id: sim-x")
    st = agent_core._get_state(sid)
    assert st["syntax_attempts"] == 0
    assert st["last_good_verilog"] == "module m; endmodule"
    agent_core.reset_session(sid)


def test_total_budget_exhaustion_mentions_last_good():
    sid = "test-total-budget"
    agent_core.reset_session(sid)
    sim_args = {"verilog_code": "module m; endmodule", "testbench_code": "tb",
                "top_module": "m"}
    agent_core._postprocess_tool_result(sid, "run_simulation", sim_args,
                                        "SIMULATION PASSED (iverilog):\nPASS")
    out = ""
    for i in range(agent_core.TOTAL_REPAIR_CAP):
        out = agent_core._postprocess_tool_result(
            sid, "run_simulation", sim_args, "SIMULATION FAILED (iverilog):\nFAIL")
        if i < agent_core.TOTAL_REPAIR_CAP - 1:
            assert "TOTAL REPAIR BUDGET EXHAUSTED" not in out
    assert "TOTAL REPAIR BUDGET EXHAUSTED" in out
    assert "passed simulation" in out  # last-good restore hint
    # Cancellations are not failures.
    agent_core._postprocess_tool_result(sid, "run_simulation", sim_args,
                                        "Simulation CANCELLED by user.")
    assert agent_core._get_state(sid)["repair_attempts"] == agent_core.TOTAL_REPAIR_CAP
    agent_core.reset_session(sid)


def test_session_isolation():
    agent_core.reset_session("sess-a")
    agent_core.reset_session("sess-b")
    args = {"verilog_code": "x", "top_module": "m"}
    agent_core._postprocess_tool_result("sess-a", "write_and_check_syntax", args,
                                        _lint_json(False))
    assert agent_core._get_state("sess-a")["syntax_attempts"] == 1
    assert agent_core._get_state("sess-b")["syntax_attempts"] == 0
    agent_core.reset_session("sess-a")
    agent_core.reset_session("sess-b")


def test_token_usage_line():
    sid = "test-tokens"
    agent_core.reset_session(sid)
    agent_core._record_usage(sid, SimpleNamespace(prompt_tokens=60,
                                                  completion_tokens=40,
                                                  total_tokens=100))
    agent_core._record_usage(sid, SimpleNamespace(prompt_tokens=10,
                                                  completion_tokens=5,
                                                  total_tokens=15))
    out = agent_core._postprocess_tool_result(sid, "run_simulation", {},
                                              "SIMULATION PASSED (iverilog):\nPASS")
    assert "tokens_used (this session): 115" in out
    # If the API ever reports a remaining budget, it wins over tokens_used.
    agent_core._record_usage(sid, SimpleNamespace(total_tokens=3,
                                                  tokens_remaining=99000))
    out = agent_core._postprocess_tool_result(sid, "run_simulation", {},
                                              "SIMULATION PASSED (iverilog):\nPASS")
    assert "tokens_remaining: 99000" in out
    agent_core.reset_session(sid)


# -- cache integration in eda_tools --------------------------------------------

class _FakeProc:
    def __init__(self, out, rc):
        self.stdout, self.stderr, self.returncode = out, "", rc


def test_write_and_check_syntax_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    monkeypatch.setenv("TOOL_CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setattr(lint.shutil, "which",
                        lambda t: "/usr/bin/verilator" if t == "verilator" else None)
    monkeypatch.setattr(lint, "tool_version", lambda t: "Verilator 5.032")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc("%%Error: %s:2:5: syntax error, unexpected endmodule\n"
                         % cmd[-1], 1)

    monkeypatch.setattr(eda_tools.subprocess, "run", fake_run)
    code = "module m;\n  wire\nendmodule\n"
    first = json.loads(eda_tools.write_and_check_syntax(code, "m"))
    assert first["ok"] is False and first["verdict"] == "syntax_error"
    assert first["tool"] == "verilator" and first["tool_version"] == "Verilator 5.032"
    assert first["errors"][0]["line"] == 2 and first["errors"][0]["col"] == 5
    assert "cached" not in first
    assert len(calls) == 1
    second = json.loads(eda_tools.write_and_check_syntax(code, "m"))
    assert second["cached"] is True and len(calls) == 1  # no re-run
    # Changed code misses the cache.
    eda_tools.write_and_check_syntax("module m; endmodule\n", "m")
    assert len(calls) == 2


def test_run_simulation_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    monkeypatch.setenv("TOOL_CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setattr(lint.shutil, "which", lambda t: "/usr/bin/x")
    monkeypatch.setattr(lint, "tool_version", lambda t: "Icarus Verilog version 12.0")

    class FakeJob:
        id = "sim-deadbeef"

        async def wait(self):
            return "SIMULATION PASSED (iverilog):\nPASS"

    class FakeManager:
        submissions = 0

        def submit(self, *a, **k):
            self.submissions += 1
            return FakeJob()

    manager = FakeManager()
    monkeypatch.setattr(eda_tools.jobs_mod, "manager", manager)

    first = asyncio.run(eda_tools.run_simulation("module m; endmodule", "module tb; endmodule", "m"))
    assert "SIMULATION PASSED" in first and "job_id: sim-deadbeef" in first
    assert manager.submissions == 1
    second = asyncio.run(eda_tools.run_simulation("module m; endmodule", "module tb; endmodule", "m"))
    assert "cached: true" in second
    assert "job_id:" not in second  # cached results have no live job
    assert manager.submissions == 1  # no new job


class _ToolCall:
    def __init__(self, name, arguments, ident="call-1"):
        self.id = ident
        self.function = type("Function", (), {
            "name": name, "arguments": json.dumps(arguments)})()


def _tool_response(name, arguments, ident="call-1", total_tokens=None):
    call = _ToolCall(name, arguments, ident)
    msg = SimpleNamespace(
        content=None, tool_calls=[call],
        model_dump=lambda exclude_none=True: {
            "role": "assistant", "tool_calls": [{
                "id": ident, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments)}}]})
    usage = None if total_tokens is None else SimpleNamespace(
        total_tokens=total_tokens, prompt_tokens=total_tokens, completion_tokens=0)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def test_token_budget_stops_before_returned_tool(monkeypatch):
    sid = "token-hard-stop"
    agent_core.reset_session(sid)
    monkeypatch.setattr(agent_core, "CHAT_TOKEN_BUDGET", 10)
    calls = []

    async def fake_create(**kwargs):
        return _tool_response("run_simulation", {
            "verilog_code": "module m; endmodule", "testbench_code": "tb",
            "top_module": "m"}, total_tokens=11)

    fake_client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=fake_create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (fake_client, "fake"))
    monkeypatch.setitem(agent_core._TOOL_FUNCS, "run_simulation",
                       lambda **kwargs: calls.append(kwargs) or "SIMULATION PASSED")

    async def run():
        return [event async for event in agent_core.chat("go", sid)]

    events = asyncio.run(run())
    assert calls == []
    assert any(event["type"] == "text" and "before tool execution" in event["content"]
               for event in events)
    agent_core.reset_session(sid)


def test_duplicate_passing_tool_call_is_suppressed(monkeypatch):
    sid = "duplicate-pass"
    agent_core.reset_session(sid)
    args = {"verilog_code": "module m; endmodule", "testbench_code": "tb",
            "top_module": "m"}
    calls = []
    responses = [
        _tool_response("run_simulation", args, "one"),
        _tool_response("run_simulation", args, "two"),
    ]

    async def fake_create(**kwargs):
        if responses:
            return responses.pop(0)
        msg = SimpleNamespace(content="done", tool_calls=None,
                              model_dump=lambda exclude_none=True:
                              {"role": "assistant", "content": "done"})
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)

    fake_client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=fake_create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (fake_client, "fake"))
    monkeypatch.setitem(agent_core._TOOL_FUNCS, "run_simulation",
                       lambda **kwargs: calls.append(kwargs) or "SIMULATION PASSED")

    async def run():
        return [event async for event in agent_core.chat("go", sid)]

    events = asyncio.run(run())
    assert len(calls) == 1
    assert any(event["type"] == "tool_result" and "DUPLICATE TOOL CALL SUPPRESSED" in event["content"]
               for event in events)
    agent_core.reset_session(sid)


def test_two_negative_slack_results_stop_timing_repairs():
    sid = "timing-stop"
    agent_core.reset_session(sid)
    agent_core._begin_chat_run(sid)
    args = {"verilog_code": "v", "top_module": "m"}
    for _ in range(2):
        assert agent_core._before_tool_call(sid, "run_silicon_compiler") is None
        agent_core._postprocess_tool_result(
            sid, "run_silicon_compiler", args,
            "Synthesis successful!\nsetupslack: -1.25 ns")
    state = agent_core._get_state(sid)
    assert state["timing_violations"] == 2
    assert "timing-closure limit" in state["run_stop_reason"]
    agent_core.reset_session(sid)


def test_synthesis_invocation_cap_blocks_fourth_call():
    sid = "synthesis-cap"
    agent_core.reset_session(sid)
    agent_core._begin_chat_run(sid)
    for _ in range(3):
        assert agent_core._before_tool_call(sid, "run_silicon_compiler") is None
        agent_core._postprocess_tool_result(
            sid, "run_silicon_compiler", {}, "Synthesis successful!\nsetupslack: 1.0 ns")
    blocked = agent_core._before_tool_call(sid, "run_silicon_compiler")
    assert blocked and "cheap synthesis/STA screen limit" in blocked
    assert agent_core._get_state(sid)["synthesis_invocations"] == 3
    agent_core.reset_session(sid)
