"""Tests for the project-owned production verification policy."""
from datetime import date
import json
import pytest

import lint
from verification_policy import (
    AssertionResult, CoverageResult, LintPolicy, LintWaiver, Severity,
    WaiverScope, apply_lint_policy, assertions_from_design_spec,
    build_verification_evidence, canonical_rule_id, coverage_requirements_from_design_spec,
    evaluate_coverage, render_sva, render_sva_for_design_spec, lint_policy_from_design_spec,
)
from evidence import ClockDomain, DesignSpec, InterfaceSpec, Requirement, VerificationPlan


def _report(*diags, ok=False):
    return {
        "tool": "verilator", "tool_version": "v", "ok": ok, "verdict": "lint_warning",
        "code_sha256": "x", "saved_to": "/src/top.v", "errors": list(diags),
        "errors_total": sum(d["severity"] == "error" for d in diags),
        "warnings_total": sum(d["severity"] == "warning" for d in diags),
        "truncated": False, "raw_stderr_tail": "",
    }


def _diag(code, severity="error", line=4, msg="finding"):
    return {"file": "/src/top.v", "line": line, "col": 2, "code": code,
            "severity": severity, "msg": msg}


def test_rule_ids_are_stable_across_tools():
    assert canonical_rule_id("WIDTHEXPAND") == "RTL-WIDTH"
    assert canonical_rule_id("ExpectedToken", "expected ';'") == "RTL-SYNTAX"
    assert canonical_rule_id(None, "Cannot find module foo") == "RTL-ELAB"
    assert canonical_rule_id("some-new-warning", "vendor detail") == "RTL-OTHER"


def test_waiver_requires_a_scope_and_is_auditable():
    waiver = LintWaiver(id="W-1", reason="third-party wrapper", author="alice",
                        scope=WaiverScope(rule_id="RTL-WIDTH", top_module="top"),
                        expires=date(2099, 1, 1), ticket="HW-42")
    dumped = waiver.model_dump(mode="json")
    assert dumped["scope"]["rule_id"] == "RTL-WIDTH"
    assert dumped["ticket"] == "HW-42"
    try:
        WaiverScope()
    except ValueError as exc:
        assert "constrain" in str(exc)
    else:
        raise AssertionError("unscoped waiver must be rejected")


def test_expired_or_mismatched_waiver_does_not_bypass_errors():
    expired = LintWaiver(id="old", reason="old", author="alice",
                         scope=WaiverScope(rule_id="RTL-SYNTAX"), expires=date(2000, 1, 1))
    policy = LintPolicy(design_class="sequential", waivers=[expired])
    out = apply_lint_policy(_report(_diag("ExpectedToken", msg="syntax error")), policy, top_module="top")
    assert out["ok"] is False
    assert out["policy"]["unwaived_errors"] == 1
    assert out["policy"]["waivers_applied"] == []


def test_scoped_active_waiver_is_recorded_and_preserves_diagnostic():
    waiver = LintWaiver(id="W-width", reason="vendor macro fixed after integration", author="bob",
                        scope=WaiverScope(rule_id="RTL-WIDTH", top_module="top", line_start=4, line_end=4),
                        expires=date(2099, 1, 1))
    policy = LintPolicy(design_class="sequential", waivers=[waiver])
    out = apply_lint_policy(_report(_diag("WIDTHEXPAND")), policy, top_module="top")
    assert out["ok"] is True
    assert out["errors"][0]["rule_id"] == "RTL-WIDTH"
    assert out["errors"][0]["waived"] is True
    assert out["errors"][0]["waiver_id"] == "W-width"
    assert out["policy"]["waived_errors"] == 1


def test_policy_can_promote_a_vendor_warning_to_blocking_error():
    policy = LintPolicy(design_class="combinational", rules={"RTL-WIDTH": Severity.ERROR})
    out = apply_lint_policy(_report(_diag("WIDTHEXPAND", severity="warning")), policy)
    assert out["ok"] is False
    assert out["policy"]["unwaived_errors"] == 1


def test_lint_build_result_compatibility_without_policy(tmp_path):
    src = tmp_path / "m.v"
    src.write_text("module m; endmodule\n")
    result = lint.build_result("verilator", "v", "module m; endmodule\n", str(src), [], 0, "")
    assert result["ok"] is True
    assert "policy" not in result


def test_lint_build_result_accepts_policy_additively(tmp_path):
    src = tmp_path / "m.v"
    src.write_text("module m; endmodule\n")
    policy = LintPolicy(design_class="sequential", rules={"RTL-WIDTH": Severity.ERROR})
    result = lint.build_result("verilator", "v", "module m; endmodule\n", str(src),
                               [_diag("WIDTHEXPAND")], 1, "", policy=policy, top_module="m")
    assert result["ok"] is False
    assert result["errors"][0]["rule_id"] == "RTL-WIDTH"
    assert result["policy"]["design_class"] == "sequential"


def test_design_spec_assertions_are_deterministic_and_renderable():
    spec = {"verification": {"assertions": [
        {"name": "no_overflow", "expression": "valid |-> !overflow", "clock": "clk",
         "disable_iff": "!rst_n", "requirement_id": "REQ-1"},
        "ready |-> eventually_done",
    ]}}
    first = assertions_from_design_spec(spec)
    second = assertions_from_design_spec(spec)
    assert [p.id for p in first] == [p.id for p in second]
    rendered = render_sva(first, module_name="dut_props")
    assert "module dut_props" in rendered
    assert "input logic clk" in rendered
    assert "no_overflow: assert property (@(posedge clk) disable iff (!rst_n) valid |-> !overflow);" in rendered
    assert "property_1: assert property (ready |-> eventually_done);" in rendered
    assert "endmodule" in rendered


def test_design_spec_coverage_uses_explicit_class_thresholds():
    reqs = coverage_requirements_from_design_spec({"design_class": "protocol"})
    assert len(reqs) == 1
    assert reqs[0].threshold == 0.95
    explicit = coverage_requirements_from_design_spec({"design_class": "protocol", "verification": {
        "coverage_requirements": [{"id": "fifo-full", "target": "full", "threshold": 0.8}]}})
    assert explicit[0].threshold == 0.8


def test_coverage_results_and_missing_requirements_are_machine_readable():
    reqs = coverage_requirements_from_design_spec({"design_class": "sequential", "verification": {
        "coverage_requirements": [{"id": "a", "target": "reset"}, {"id": "b", "target": "backpressure"}]}})
    result = CoverageResult.from_counts("a", 9, 10, reqs[0].threshold)
    gate = evaluate_coverage(reqs, [result], design_class="sequential")
    assert gate["passed"] is False
    assert gate["missing"] == ["b"]
    assert result.percentage == 0.9 and result.passed is False


def test_verification_evidence_requires_lint_assertions_and_coverage():
    policy = LintPolicy(design_class="sequential")
    lint_report = apply_lint_policy(_report(ok=True), policy)
    coverage = CoverageResult.from_counts("seq", 95, 100, 0.95)
    evidence = build_verification_evidence(
        design_class="sequential", lint_report=lint_report,
        assertions=[AssertionResult(property_id="p", status="proven", tool="sby")],
        coverage=[coverage],
    )
    assert evidence.overall_pass is True
    encoded = json.loads(evidence.model_dump_json())
    assert encoded["schema_version"] == "verification-evidence/v1"
    assert encoded["gates"] == {"lint": True, "assertions": True, "functional_coverage": True}


def test_failed_assertion_or_empty_coverage_blocks_evidence():
    evidence = build_verification_evidence(
        design_class="combinational", lint_report={"ok": True, "errors": [], "warnings_total": 0},
        assertions=[AssertionResult(property_id="p", status="bounded")], coverage=[]
    )
    assert evidence.overall_pass is False
    assert evidence.gates["assertions"] is False
    assert evidence.gates["functional_coverage"] is False


def test_real_design_spec_drives_policy_sva_and_coverage():
    spec = DesignSpec(
        design_id="fifo", name="FIFO", top_module="fifo_top",
        interfaces=[InterfaceSpec(name="clk", direction="input"),
                    InterfaceSpec(name="valid", direction="input"),
                    InterfaceSpec(name="ready", direction="output")],
        clock_domains=[ClockDomain(name="core", clock_port="clk")],
        requirements=[Requirement(id="handshake", text="valid transfers only when ready")],
        verification=VerificationPlan(
            design_class="protocol", coverage_target_percent=95,
            assertions=[{"name": "stable_valid", "expression": "valid |-> ready", "clock": "clk",
                         "requirement_id": "handshake"}],
            coverage_requirements=[{"id": "handshake", "target": "valid/ready transfer",
                                    "requirement_id": "handshake"}],
            lint_rules={"RTL-WIDTH": "error"},
        ),
    )
    policy = lint_policy_from_design_spec(spec)
    assert policy.design_class == "protocol"
    assert policy.coverage_threshold == 0.95
    assert policy.severity_for("RTL-WIDTH") is Severity.ERROR
    props = assertions_from_design_spec(spec)
    assert len(props) == 1 and props[0].requirement_id == "handshake"
    checker = render_sva_for_design_spec(spec)
    assert "bind fifo_top agent_properties agent_properties_bound" in checker
    assert ".clk(clk)" in checker and ".valid(valid)" in checker
    requirements = coverage_requirements_from_design_spec(spec)
    assert requirements[0].id == "handshake" and requirements[0].threshold == 0.95


def test_real_design_spec_rejects_unscoped_waivers_and_bad_policy_fields():
    with pytest.raises(ValueError):
        VerificationPlan(waivers=[{"id": "w", "reason": "x", "author": "a",
                                  "scope": {}, "expires": "2099-01-01"}])
    with pytest.raises(ValueError):
        VerificationPlan(lint_rules={"RTL-WIDTH": "fatal"})
