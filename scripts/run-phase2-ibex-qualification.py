#!/usr/bin/env python3
"""Emit digest-bound Phase 2 Ibex compliance and production-lint evidence."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ibex_qualification_policy import classify_lint, classify_rv32i_compliance
from lint import parse_verilator_text
from multifile_eda import materialize_manifest, verilator_lint_argv

IBEX_COMMIT = "654ac71f91f7da5dc71942c9ba8d3103666c484e"
SUITE_COMMIT = "844c6660ef3f0d9b96957991109dfd80cc4938e2"
TOOLCHAIN_SHA256 = "7da91ebcef42d60324d58d7b099855debac6c99172244df49d06bf4d9f461f5f"
QUALIFICATION = Path("/mnt/cold-hdd/riscv-qualification")


def write_report(name: str, report: dict) -> None:
    target = ROOT / "docs" / "evidence" / name
    target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"{name}: {report['outcome']} ({report['report_digest']})")


def main() -> int:
    baseline = json.loads((ROOT / "docs/evidence/phase2-ibex-minimal.json").read_text())
    project = materialize_manifest(baseline["hardware_project"])
    completed = subprocess.run(
        verilator_lint_argv(project), cwd=project.root, capture_output=True,
        text=True, timeout=300, check=False,
    )
    diagnostics = parse_verilator_text(completed.stdout + completed.stderr)
    lint_report = classify_lint(diagnostics, manifest_digest=project.digest)
    lint_report.update({
        "tool": "verilator",
        "tool_returncode": completed.returncode,
        "source_report_digest": baseline["lint"]["report_digest"],
    })
    # Recalculate after adding execution provenance.
    import hashlib
    lint_report.pop("report_digest", None)
    body = json.dumps(lint_report, sort_keys=True, separators=(",", ":")).encode()
    lint_report["report_digest"] = hashlib.sha256(body).hexdigest()
    write_report("phase2-ibex-production-lint.json", lint_report)

    log = (QUALIFICATION / "ibex-rv32i-compliance.log").read_text()
    compliance = classify_rv32i_compliance(
        log, ibex_commit=IBEX_COMMIT, suite_commit=SUITE_COMMIT,
        toolchain_sha256=TOOLCHAIN_SHA256,
    )
    write_report("phase2-ibex-rv32i-compliance.json", compliance)
    return 0 if lint_report["ok"] and compliance["ok"] and completed.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
