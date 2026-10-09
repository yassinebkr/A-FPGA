"""Bounded native-SystemVerilog RVFI checks for the pinned Ibex profile."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from hardware_project import validate_manifest
from ibex_rvfi_formal import _CHECKER
from multifile_eda import materialize_manifest


SCHEMA = "ibex-rvfi-native-formal/v1"
DEFAULT_IMAGE = (
    "ghcr.io/librelane/librelane@sha256:"
    "f91b21d75f79871f9ccf37451020b5d2f7b3236881a997ff709c561e8280a30f"
)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _tail(path: Path, limit: int = 12000) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(errors="replace")
    return text[-limit:]


def run_native_bounded_rvfi(
    manifest: dict[str, Any], *, workdir: Path, depth: int = 12,
    timeout: int = 600, image: str = DEFAULT_IMAGE,
) -> dict[str, Any]:
    """Elaborate native SV with sv-elab and run bounded SAT plus reachability."""
    checked = validate_manifest(manifest, require_artifacts=True)
    if checked["top_module"] != "ibex_core":
        raise ValueError("RVFI profile must target ibex_core")
    if checked.get("defines", {}).get("RVFI") != "1":
        raise ValueError("RVFI must be explicitly enabled")
    if not isinstance(depth, int) or isinstance(depth, bool) or not 2 <= depth <= 100:
        raise ValueError("depth must be between 2 and 100")

    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    sources = materialize_manifest(checked, destination=workdir / "sources")
    top_rel = next((item for item in checked["compile_order"]
                    if item.endswith("/ibex_core.sv")), None)
    if top_rel is None:
        raise ValueError("manifest does not contain the pinned Ibex top source")
    native = (sources.root / top_rel).read_text()
    marker = "\nendmodule"
    offset = native.rfind(marker)
    if offset < 0:
        raise ValueError("Ibex top has no final endmodule")
    instrumented = native[:offset] + "\n" + _CHECKER + native[offset:]
    instrumented_path = workdir / "ibex_core_formal.sv"
    instrumented_path.write_text(instrumented)

    source_args = ["/work/ibex_core_formal.sv" if item == top_rel
                   else f"/source/{item}" for item in checked["compile_order"]]
    include_args = [f"-I/source/{item}" for item in checked.get("include_dirs", [])]
    define_args = [f"-D{name}" if value is None else f"-D{name}={value}"
                   for name, value in sorted(checked.get("defines", {}).items())]
    read = "read_slang " + " ".join(source_args + ["--top", "ibex_core"]
                                      + include_args + define_args + ["-DFORMAL=1"])
    common = "\n".join([
        read, "prep -top ibex_core -flatten", "async2sync", "memory_map",
        "opt_clean -purge", "setundef -undriven -anyseq", "opt_clean -purge",
    ])
    bmc_script = common + (f"\nsat -seq {depth} -prove-asserts -set-assumes "
                           "-verify -timeout 300\n")
    cover_script = common + (f"\nsat -seq {depth} -set-assumes "
                             f"-set-at {depth} rvfi_valid 1 "
                             "-show rvfi_valid -show rvfi_order\n")
    (workdir / "bmc.ys").write_text(bmc_script)
    (workdir / "cover.ys").write_text(cover_script)

    source_mount = f"type=bind,src={sources.root},dst=/source,readonly"
    work_mount = f"type=bind,src={workdir},dst=/work"
    base = ["docker", "run", "--rm", "--network=none", "--read-only",
            "--mount", source_mount, "--mount", work_mount, image,
            "yosys", "-Q", "-q", "-m", "slang"]
    commands = [base + ["-l", "/work/bmc.log", "-s", "/work/bmc.ys"],
                base + ["-l", "/work/cover.log", "-s", "/work/cover.ys"]]
    results = []
    try:
        for command in commands:
            results.append(subprocess.run(command, cwd=workdir, capture_output=True,
                                          text=True, timeout=timeout, check=False))
    except subprocess.TimeoutExpired:
        status, reason = "inconclusive", f"formal run timed out after {timeout}s"
    else:
        bmc_ok = results[0].returncode == 0
        cover_log = (workdir / "cover.log").read_text(errors="replace")
        cover_ok = results[1].returncode == 0 and "SAT solving finished - model found:" in cover_log
        if not bmc_ok:
            status, reason = "failed", "a bounded RVFI assertion failed or the engine errored"
        elif not cover_ok:
            status, reason = "inconclusive", "assertions held but retirement reachability was not demonstrated"
        else:
            status, reason = "bounded", "assertions held and retirement was reachable within the bound"

    report = {
        "schema": SCHEMA,
        "status": status,
        "claim": "bounded RVFI interface consistency; not ISA compliance or an unbounded proof",
        "reason": reason,
        "hardware_project_digest": checked["digest"],
        "instrumented_top_sha256": hashlib.sha256(instrumented.encode()).hexdigest(),
        "checker_sha256": hashlib.sha256(_CHECKER.encode()).hexdigest(),
        "depth": depth,
        "environment_assumptions": [
            "reset asserted for first sampled cycle and deasserted thereafter",
            "boot address is halfword aligned", "CHERIoT mode is disabled",
            "instruction fetch is enabled", "undriven disabled-feature cones are symbolic",
        ],
        "properties": [
            "RVFI IXL identifies RV32", "retired PC is halfword aligned",
            "RVFI memory read/write masks are mutually exclusive",
            "x0 destination write data is zero",
            "RVFI order increments across retirements",
        ],
        "non_vacuity": "rvfi_valid reachable at the final bounded step",
        "toolchain": {
            "container": image, "yosys": "0.62", "frontend": "sv-elab/read_slang",
            "engine": "Yosys SAT",
            "note": "SMT2 export was rejected by a Yosys backend assertion; no Z3 claim is made",
        },
        "commands": commands,
        "returncodes": [item.returncode for item in results],
        "bmc_log_tail": _tail(workdir / "bmc.log"),
        "cover_log_tail": _tail(workdir / "cover.log"),
    }
    report["report_digest"] = _canonical_digest(report)
    (workdir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


__all__ = ["DEFAULT_IMAGE", "SCHEMA", "run_native_bounded_rvfi"]
