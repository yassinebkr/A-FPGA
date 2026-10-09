from ibex_qualification_policy import (
    EXPECTED_RV32I_FAILURES, IBEX_MANIFEST_DIGEST,
    classify_lint, classify_rv32i_compliance,
)


def test_lint_policy_accepts_exact_reviewed_baseline():
    diagnostics = [
        {"severity": "warning", "code": "PINCONNECTEMPTY", "file": "x/ibex_core.sv"}
        for _ in range(36)
    ] + [
        {"severity": "warning", "code": "UNUSEDPARAM", "file": "x/ibex_pkg.sv"}
        for _ in range(47)
    ]
    assert classify_lint(diagnostics, manifest_digest=IBEX_MANIFEST_DIGEST)["ok"]


def test_lint_policy_rejects_new_warning():
    report = classify_lint(
        [{"severity": "warning", "code": "WIDTH", "file": "x/ibex_core.sv"}],
        manifest_digest=IBEX_MANIFEST_DIGEST,
    )
    assert not report["ok"]
    assert report["unapproved"]


def test_compliance_requires_exact_known_exclusions():
    ok_lines = "\n".join(f"Check T-{idx} ... OK" for idx in range(44))
    fail_lines = "\n".join(f"Check {name} ... FAIL" for name in EXPECTED_RV32I_FAILURES)
    report = classify_rv32i_compliance(
        f"{ok_lines}\n{fail_lines}\nFAIL: 4/48\n",
        ibex_commit="i", suite_commit="s", toolchain_sha256="t",
    )
    assert report["ok"]
    assert report["outcome"] == "success_with_known_exclusions"


def test_compliance_rejects_unreviewed_failure():
    report = classify_rv32i_compliance(
        "Check I-ADD-01 ... FAIL\nFAIL: 4/48\n",
        ibex_commit="i", suite_commit="s", toolchain_sha256="t",
    )
    assert not report["ok"]
