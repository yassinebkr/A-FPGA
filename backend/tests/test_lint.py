"""Golden-fixture tests for the structured lint layer (lint.py) and the
tool-result cache (tool_cache.py). Fixtures mirror real tool output:
verilator 5.032 text (captured from the real binary), verilator SARIF
(after test_regress/t/t_sarif.sarif.out), slang --diag-json (schema from
slang's JsonDiagnosticClient.cpp), iverilog and yosys text."""
import json

import lint
import tool_cache


# -- golden fixtures ----------------------------------------------------------

VERILATOR_TEXT_ERROR = """\
%Error: broken.v:9:9: syntax error, unexpected else
    9 |         else
      |         ^~~~
%Error: Exiting due to 1 error(s)
"""

VERILATOR_TEXT_WARNINGS = """\
%Warning-WIDTHEXPAND: widths.v:7:15: Operator ADD expects 32 bits on the LHS, but LHS's VARREF 'a' generates 8 bits.
                                   : ... note: In instance 'widths'
    7 |         y = a + b;
      |               ^
%Warning-WIDTHEXPAND: widths.v:7:15: Operator ADD expects 32 bits on the RHS, but RHS's VARREF 'b' generates 8 bits.
- Verilator: Walltime 0.003 s (elab=0.000, cvt=0.001); cpu 0.003 s on 1 threads
"""

VERILATOR_SARIF = json.dumps({
    "$schema": "https://json.schemastore.org/sarif-2.1.0-rtm.5.json",
    "version": "2.1.0",
    "runs": [{
        "tool": {"driver": {"name": "Verilator", "version": "5.038"}},
        "invocations": [{"commandLine": "--lint-only -Wall t.v", "executionSuccessful": True}],
        "results": [
            {
                "level": "warning",
                "ruleId": "WIDTHEXPAND",
                "ruleIndex": 0,
                "message": {
                    "text": "Operator ADD expects 32 bits on the LHS, but LHS's VARREF 'a' generates 8 bits.\n"
                            "... For warning description see https://verilator.org/warn/WIDTHEXPAND?v=latest\n"
                            "... Use \"/* verilator lint_off WIDTHEXPAND */\" and lint_on around source to disable this message.",
                },
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": "file:///tmp/build/widths/widths.v"},
                        "region": {"sourceLanguage": "systemverilog",
                                   "startLine": 7, "startColumn": 15, "endColumn": 16},
                    },
                }],
            },
            {
                "level": "error",
                "message": {"text": "Cannot find file containing module: 'missing_mod'"},
                "locations": [],
            },
        ],
    }],
})

SLANG_DIAG_JSON = json.dumps([
    {
        "severity": "error",
        "message": "expected ';'",
        "code": "ExpectedToken",
        "location": "broken.v:9:9",
    },
    {
        "severity": "warning",
        "message": "width of 'a' (8 bits) is implicitly extended to 32 bits",
        "code": "WidthExpand",
        "optionName": "width-expand",
        "location": "widths.v:7:15",
        "notes": [{"message": "declared here", "code": "None", "location": "widths.v:2:18"}],
    },
])

IVERILOG_TEXT = """\
broken.v:9: syntax error
broken.v:11: error: malformed statement
I give up.
"""

YOSYS_TEXT = """\
2. Executing Verilog-2005 frontend: broken.v
Warning: Identifier `foo' is implicitly declared at broken.v:5.
ERROR: syntax error, unexpected TOK_ID at broken.v:9.
"""


# -- verilator text parser ------------------------------------------------------

def test_verilator_text_error():
    diags = lint.parse_verilator_text(VERILATOR_TEXT_ERROR)
    assert len(diags) == 1  # "Exiting due to" summary line is dropped
    d = diags[0]
    assert d["severity"] == "error"
    assert d["file"] == "broken.v" and d["line"] == 9 and d["col"] == 9
    assert d["code"] is None
    assert "syntax error" in d["msg"]


def test_verilator_text_warnings():
    diags = lint.parse_verilator_text(VERILATOR_TEXT_WARNINGS)
    assert len(diags) == 2  # continuation/caret/report lines ignored
    assert all(d["severity"] == "warning" for d in diags)
    assert diags[0]["code"] == "WIDTHEXPAND"
    assert diags[0]["line"] == 7 and diags[0]["col"] == 15
    assert diags[0]["msg"].endswith("generates 8 bits.")


def test_verilator_sarif():
    diags = lint.parse_verilator_sarif(VERILATOR_SARIF)
    assert len(diags) == 2
    warn = diags[0]
    assert warn["severity"] == "warning" and warn["code"] == "WIDTHEXPAND"
    assert warn["file"] == "/tmp/build/widths/widths.v"  # file:// stripped
    assert warn["line"] == 7 and warn["col"] == 15
    # only the first line of message.text; "... see <url>" trailers stripped
    assert "..." not in warn["msg"]
    err = diags[1]
    assert err["severity"] == "error" and err["line"] is None
    assert lint.parse_verilator_sarif("not json") == []


def test_slang_diag_json():
    diags = lint.parse_slang_json(SLANG_DIAG_JSON)
    assert len(diags) == 2
    err = diags[0]
    assert err["severity"] == "error" and err["code"] == "ExpectedToken"
    assert err["file"] == "broken.v" and err["line"] == 9 and err["col"] == 9
    warn = diags[1]
    assert warn["severity"] == "warning"
    assert warn["code"] == "width-expand"  # optionName preferred over code
    assert lint.parse_slang_json("{}") == []  # not an array


def test_iverilog_text():
    diags = lint.parse_iverilog_text(IVERILOG_TEXT)
    assert len(diags) == 2  # "I give up." dropped
    assert diags[0] == {"file": "broken.v", "line": 9, "col": None,
                        "code": None, "severity": "error", "msg": "syntax error"}
    assert diags[1]["severity"] == "error" and diags[1]["line"] == 11


def test_yosys_text():
    diags = lint.parse_yosys_text(YOSYS_TEXT, default_file="broken.v")
    assert len(diags) == 2
    assert diags[0]["severity"] == "warning"
    assert diags[0]["file"] == "broken.v" and diags[0]["line"] == 5
    assert diags[1]["severity"] == "error" and diags[1]["line"] == 9


# -- normalization --------------------------------------------------------------

def _diag(severity="error", line=1, msg="boom"):
    return {"file": "m.v", "line": line, "col": None, "code": None,
            "severity": severity, "msg": msg}


def test_build_result_schema_ok(tmp_path):
    src = tmp_path / "m.v"
    src.write_text("module m; endmodule\n")
    result = lint.build_result("verilator", "Verilator 5.032", "module m; endmodule\n",
                               str(src), [], 0, "")
    assert result["ok"] is True and result["verdict"] == "ok"
    assert result["tool"] == "verilator" and result["tool_version"] == "Verilator 5.032"
    assert result["errors"] == [] and result["errors_total"] == 0
    assert result["warnings_total"] == 0 and result["truncated"] is False
    assert result["raw_stderr_tail"] == ""
    assert len(result["code_sha256"]) == 64


def test_build_result_verdicts(tmp_path):
    src = tmp_path / "m.v"
    src.write_text("module m;\nendmodule\n")
    r = lint.build_result("verilator", "v", "x", str(src), [_diag(msg="syntax error, unexpected x")], 1, "raw")
    assert r["verdict"] == "syntax_error" and r["ok"] is False
    r = lint.build_result("verilator", "v", "x", str(src), [_diag(msg="Cannot find module 'foo'")], 1, "raw")
    assert r["verdict"] == "elaboration_error" and r["ok"] is False
    r = lint.build_result("verilator", "v", "x", str(src), [_diag(severity="warning")], 0, "raw")
    assert r["verdict"] == "lint_warning" and r["ok"] is True  # warnings don't block
    r = lint.build_result("yosys", "v", "x", str(src), [], 1, "some crash")
    assert r["verdict"] == "elaboration_error"
    assert r["raw_stderr_tail"] == "some crash"  # unparseable failure keeps the raw tail


def test_build_result_dedupe_and_cap(tmp_path):
    src = tmp_path / "m.v"
    src.write_text("\n".join(f"// line {i}" for i in range(1, 30)))
    diags = ([_diag("error", line=i) for i in range(1, 6)]
             + [_diag("warning", line=i) for i in range(6, 12)]
             + [_diag("warning", line=20, msg="repeated")] * 3)
    r = lint.build_result("iverilog", "v", "x", str(src), diags, 1, "")
    assert r["errors_total"] == 5 and r["warnings_total"] == 9  # raw counts (6+3)
    assert len(r["errors"]) == lint.MAX_DIAGS and r["truncated"] is True
    assert [d["severity"] for d in r["errors"][:5]] == ["error"] * 5  # errors first
    assert [d["line"] for d in r["errors"][:5]] == [1, 2, 3, 4, 5]  # source order


def test_build_result_dedupe(tmp_path):
    src = tmp_path / "m.v"
    src.write_text("module m; endmodule\n")
    diags = [_diag("warning", line=3, msg="repeated")] * 3
    r = lint.build_result("iverilog", "v", "x", str(src), diags, 0, "")
    assert r["warnings_total"] == 3
    assert len(r["errors"]) == 1
    assert r["errors"][0]["msg"].endswith("(×3)")


def test_msg_truncation_keeps_ends():
    msg = ("Operator ASSIGN expects 4294967296 bits on the RHS " + "x" * 400
           + " but generates 32 bits.")
    out = lint.truncate_msg(msg)
    assert len(out) <= lint.MSG_CAP
    assert out.startswith("Operator ASSIGN expects 4294967296 bits")
    assert out.endswith("generates 32 bits.")
    assert "…" in out


def test_snippet_caret(tmp_path):
    src = tmp_path / "m.v"
    src.write_text("module m;\n    assign y = a & b\nendmodule\n")
    r = lint.build_result("verilator", "v", "x", str(src),
                          [{"file": str(src), "line": 2, "col": 18, "code": None,
                            "severity": "error", "msg": "syntax error"}], 1, "")
    snippet = r["errors"][0]["snippet"]
    assert "2 |     assign y = a & b" in snippet
    caret_line = snippet.splitlines()[1]
    assert caret_line == "  | " + " " * 17 + "^"  # caret under col 18 ('&')


def test_verilator_version_gate():
    assert not lint.verilator_supports_sarif("Verilator 5.032 2025-01-01 rev v5.032")
    assert lint.verilator_supports_sarif("Verilator 5.038 2025-07-08 rev v5.038")
    assert lint.verilator_supports_sarif("Verilator 5.040")
    assert not lint.verilator_supports_sarif("")


# -- tool-result cache -----------------------------------------------------------

def test_tool_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_CACHE_DB", str(tmp_path / "cache.db"))
    key = tool_cache.make_key("verilator", "5.032", "module m; endmodule")
    assert tool_cache.get(key) is None
    tool_cache.put(key, '{"ok": true}')
    assert tool_cache.get(key) == '{"ok": true}'
    # Key inputs: tool name, version, verilog, and testbench all matter.
    assert tool_cache.make_key("iverilog", "5.032", "module m; endmodule") != key
    assert tool_cache.make_key("verilator", "5.038", "module m; endmodule") != key
    assert tool_cache.make_key("verilator", "5.032", "module m2; endmodule") != key
    assert (tool_cache.make_key("verilator", "5.032", "module m; endmodule", "tb")
            != key)
    # Corrupt/missing DB never raises.
    monkeypatch.setenv("TOOL_CACHE_DB", str(tmp_path / "missing" / "cache.db"))
    assert tool_cache.get(key) is None
