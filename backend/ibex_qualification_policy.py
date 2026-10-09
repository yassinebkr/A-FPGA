"""Fail-closed policy checks for the pinned Phase 2 Ibex qualification."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


IBEX_MANIFEST_DIGEST = "367870e1a90f85b1b82ed2d067c26154b0dbfee0e9c95abc302ee9806a03c394"
EXPECTED_RV32I_FAILURES = {
    "I-EBREAK-01", "I-ECALL-01", "I-MISALIGN_JMP-01", "I-MISALIGN_LDST-01",
}
_LINT_POLICY = {
    "PINCONNECTEMPTY": {
        "ibex_icache.sv", "ibex_cs_registers.sv", "ibex_if_stage.sv",
        "ibex_load_store_unit.sv", "ibex_core.sv",
    },
    "UNUSEDPARAM": {"prim_cipher_pkg.sv", "prim_secded_pkg.sv", "ibex_pkg.sv"},
}


def _digest(report: dict[str, Any]) -> str:
    body = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def classify_lint(diagnostics: Iterable[dict[str, Any]], *,
                  manifest_digest: str) -> dict[str, Any]:
    items = list(diagnostics)
    unapproved = []
    for diagnostic in items:
        code = diagnostic.get("code")
        basename = Path(str(diagnostic.get("file", ""))).name
        if (diagnostic.get("severity") != "warning" or
                basename not in _LINT_POLICY.get(str(code), set())):
            unapproved.append(diagnostic)
    counts = Counter(str(item.get("code")) for item in items)
    expected_counts = {"PINCONNECTEMPTY": 36, "UNUSEDPARAM": 47}
    exact_baseline = manifest_digest == IBEX_MANIFEST_DIGEST and dict(counts) == expected_counts
    report = {
        "schema": "ibex-production-lint-policy/v1",
        "hardware_project_digest": manifest_digest,
        "policy": "reviewed upstream diagnostics only; errors and new code/file pairs fail",
        "diagnostics_total": len(items),
        "counts_by_code": dict(sorted(counts.items())),
        "expected_counts_by_code": expected_counts,
        "unapproved": unapproved,
        "ok": exact_baseline and not unapproved,
        "outcome": "success" if exact_baseline and not unapproved else "failure",
    }
    report["report_digest"] = _digest(report)
    return report


def classify_rv32i_compliance(log: str, *, ibex_commit: str,
                              suite_commit: str,
                              toolchain_sha256: str) -> dict[str, Any]:
    passed: set[str] = set()
    failed: set[str] = set()
    current: str | None = None
    for line in log.splitlines():
        match = re.match(r"^Check\s+(\S+)", line)
        if match:
            current = match.group(1)
            for expected in EXPECTED_RV32I_FAILURES:
                if line[5:].lstrip().startswith(expected):
                    current = expected
                    break
        if line.endswith("... OK") and current:
            passed.add(current)
            current = None
        elif line.endswith("... FAIL") and current:
            failed.add(current)
            current = None
    summary = re.search(r"^FAIL:\s+(\d+)/(\d+)$", log, re.MULTILINE)
    exact = bool(summary and int(summary.group(1)) == 4 and int(summary.group(2)) == 48)
    known_only = failed == EXPECTED_RV32I_FAILURES
    report = {
        "schema": "ibex-rv32i-compliance/v1",
        "ibex_commit": ibex_commit,
        "suite_commit": suite_commit,
        "toolchain_archive_sha256": toolchain_sha256,
        "isa": "rv32i",
        "tests_total": 48,
        "passed": len(passed),
        "failed": sorted(failed),
        "known_upstream_exclusions": sorted(EXPECTED_RV32I_FAILURES),
        "policy": "all ordinary tests pass; only the four exclusions accepted by pinned Ibex CI are allowed",
        "ok": exact and known_only and len(passed) == 44,
        "outcome": "success_with_known_exclusions" if exact and known_only and len(passed) == 44 else "failure",
    }
    report["report_digest"] = _digest(report)
    return report
