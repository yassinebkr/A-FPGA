"""Deterministic, provenance-bound pre-P&R physical acceptance gate."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from physical_evidence import PhysicalEvidenceError, collect_physical_evidence

SCHEMA = "physical-acceptance-gate/v1"
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_SLACK = re.compile(rf"^\s*worst\s+slack\s+(?P<kind>max|min)\s+(?P<value>{_NUMBER})\s*$", re.I)
_FANOUT_ROW = re.compile(r"^\s*(?P<net>\S+)\s+(?P<fanout>\d+)\s*$")
_DIODE_ENDPOINT = re.compile(r"^ANTENNA_[0-9]+/DIODE$")


class PhysicalGateError(ValueError):
    """Required evidence is absent, malformed, or internally inconsistent."""


def _read(path_value: str | Path, category: str) -> tuple[Path, bytes]:
    path = Path(path_value)
    if not path.is_file():
        raise PhysicalGateError(f"missing {category} report: {path}")
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PhysicalGateError(f"malformed {category} report (not UTF-8): {path}") from exc
    return path, raw


def _hash_file(path_value: str | Path, category: str) -> dict[str, Any]:
    path, raw = _read(path_value, category)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw)}


def _parse_slack(path_value: str | Path, expected_kind: str) -> tuple[float, dict[str, Any]]:
    path, raw = _read(path_value, f"{expected_kind} slack")
    text = raw.decode("utf-8")
    match = _SLACK.fullmatch(text)
    if match is None or match.group("kind").lower() != expected_kind:
        raise PhysicalGateError(f"malformed {expected_kind} slack report: {path}")
    value = float(match.group("value"))
    if not math.isfinite(value):
        raise PhysicalGateError(f"non-finite {expected_kind} slack: {value}")
    return value, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                   "bytes": len(raw)}


def _parse_high_fanout(path_value: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path, raw = _read(path_value, "high fanout")
    lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not lines or lines[0].split() != ["net", "fanout"]:
        raise PhysicalGateError(f"malformed high fanout report header: {path}")
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines[1:], 2):
        match = _FANOUT_ROW.fullmatch(line)
        if match is None:
            raise PhysicalGateError(f"malformed high fanout report at line {line_no}: {path}")
        rows.append({"net": match.group("net"), "fanout": int(match.group("fanout"))})
    return rows, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                  "bytes": len(raw)}


def _parse_zero_or_entries(
    path_value: str | Path,
    category: str,
    *,
    empty_proven_clean: bool,
) -> tuple[list[str], dict[str, Any]]:
    path, raw = _read(path_value, category)
    lines = [line.strip() for line in raw.decode("utf-8").splitlines() if line.strip()]
    clean = {"0", "0 violations", f"no {category.lower()} violations"}
    if lines and len(lines) == 1 and lines[0].lower().rstrip(".") in clean:
        lines = []
    if not lines and not empty_proven_clean:
        raise PhysicalGateError(
            f"empty {category} report lacks manifest-proven successful check: {path}")
    if any(len(item) > 1024 or "\x00" in item for item in lines):
        raise PhysicalGateError(f"malformed {category} report: {path}")
    return lines, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                   "bytes": len(raw)}


def evaluate_physical_gate(
    report_paths: Mapping[str, str | Path],
    *,
    artifact_path: str | Path,
    reviewed_sdc_path: str | Path,
    consumed_sdc_path: str | Path,
    manifest_path: str | Path,
    run_record_path: str | Path,
    physical_netlist_path: str | Path,
    toolchain: Mapping[str, str],
    target: Mapping[str, str],
    physical_only_endpoints: Sequence[str] = (),
) -> dict[str, Any]:
    """Evaluate exact reports. Any absent category or ambiguous waiver fails closed."""
    required = {
        "drv_violators", "floating_nets", "overdriven_nets",
        "unconstrained_endpoints", "setup_slack", "hold_slack",
        "high_fanout", "drc", "antenna",
    }
    missing = sorted(required - report_paths.keys())
    if missing:
        raise PhysicalGateError("missing required report path(s): " + ", ".join(missing))
    try:
        electrical = collect_physical_evidence({
            name: report_paths[name]
            for name in ("drv_violators", "floating_nets", "overdriven_nets",
                         "unconstrained_endpoints")
        })
    except PhysicalEvidenceError as exc:
        raise PhysicalGateError(str(exc)) from exc

    netlist_path, netlist_raw = _read(physical_netlist_path, "physical netlist")
    netlist = netlist_raw.decode("utf-8")
    classifications: dict[str, str] = {}
    for endpoint in physical_only_endpoints:
        if not _DIODE_ENDPOINT.fullmatch(endpoint):
            raise PhysicalGateError(
                f"physical-only endpoint must name one inserted antenna diode: {endpoint!r}")
        if endpoint in classifications:
            raise PhysicalGateError(f"duplicate physical-only endpoint: {endpoint}")
        instance = endpoint.removesuffix("/DIODE")
        declaration = re.compile(
            rf"(?m)^\s*sky130_fd_sc_hd__diode_[0-9]+\s+{re.escape(instance)}\s*\(")
        if declaration.search(netlist) is None:
            raise PhysicalGateError(
                f"physical-only endpoint has no matching diode in physical netlist: {endpoint}")
        classifications[endpoint] = "inserted_antenna_diode"
    unmatched = sorted(set(classifications) - set(electrical.unconstrained_endpoints))
    if unmatched:
        raise PhysicalGateError(
            "physical-only endpoint classification did not match a report: " + ", ".join(unmatched))
    functional_unconstrained = sorted(
        set(electrical.unconstrained_endpoints) - set(classifications))

    setup_slack, setup_report = _parse_slack(report_paths["setup_slack"], "max")
    hold_slack, hold_report = _parse_slack(report_paths["hold_slack"], "min")
    fanout, fanout_report = _parse_high_fanout(report_paths["high_fanout"])
    manifest_info = _hash_file(manifest_path, "manifest")
    run_record_info = _hash_file(run_record_path, "run record")
    try:
        run_record = json.loads(Path(run_record_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise PhysicalGateError("run record is not valid JSON") from exc
    if not isinstance(run_record, dict) or not run_record.get("pnr_completed"):
        raise PhysicalGateError("run record does not prove completed P&R")
    if run_record.get("manifest_sha256") != manifest_info["sha256"]:
        raise PhysicalGateError("run record manifest digest does not match supplied manifest")
    artifact_info = _hash_file(artifact_path, "RTL artifact")
    if run_record.get("source_artifact_sha256") != artifact_info["sha256"]:
        raise PhysicalGateError("run record RTL digest does not match supplied artifact")
    recorded_tools = run_record.get("tool_versions")
    if not isinstance(recorded_tools, dict) or not recorded_tools:
        raise PhysicalGateError("run record has no tool-version evidence")
    if any(toolchain.get(name) != version for name, version in recorded_tools.items()):
        raise PhysicalGateError("toolchain does not match run record")
    if run_record.get("target") != target.get("target"):
        raise PhysicalGateError("target does not match run record")
    job_root = Path(manifest_path).resolve().parent
    for category, path_value in report_paths.items():
        try:
            Path(path_value).resolve().relative_to(job_root)
        except ValueError as exc:
            raise PhysicalGateError(
                f"{category} report is outside the manifest job root") from exc

    drc_clean = run_record.get("detailed_route_violations") == 0
    antenna_clean = run_record.get("remaining_antenna_violations") == 0
    drc, drc_report = _parse_zero_or_entries(
        report_paths["drc"], "DRC", empty_proven_clean=drc_clean)
    antenna, antenna_report = _parse_zero_or_entries(
        report_paths["antenna"], "antenna", empty_proven_clean=antenna_clean)

    provenance = {
        "rtl_artifact": artifact_info,
        "reviewed_sdc": _hash_file(reviewed_sdc_path, "reviewed SDC"),
        "consumed_sdc": _hash_file(consumed_sdc_path, "consumed SDC"),
        "manifest": manifest_info,
        "run_record": run_record_info,
        "physical_netlist": {
            "path": str(netlist_path),
            "sha256": hashlib.sha256(netlist_raw).hexdigest(),
            "bytes": len(netlist_raw),
        },
        "toolchain": dict(sorted(toolchain.items())),
        "target": dict(sorted(target.items())),
    }
    constraints_match = (
        provenance["reviewed_sdc"]["sha256"] == provenance["consumed_sdc"]["sha256"])
    violation_counts = {
        "setup": int(setup_slack < 0),
        "hold": int(hold_slack < 0),
        "max_slew": sum(v.category == "max_slew" and v.status == "unwaived"
                        for v in electrical.violations),
        "max_capacitance": sum(v.category == "max_capacitance" and v.status == "unwaived"
                               for v in electrical.violations),
        "floating_nets": len(electrical.floating_nets),
        "overdriven_nets": len(electrical.overdriven_nets),
        "unconstrained_functional_endpoints": len(functional_unconstrained),
        "drc": len(drc),
        "antenna": len(antenna),
        "constraint_mismatch": int(not constraints_match),
    }
    blockers = [name for name, count in violation_counts.items() if count]
    reports = {
        "electrical": electrical.canonical_data(),
        "setup_slack": setup_report,
        "hold_slack": hold_report,
        "high_fanout": fanout_report,
        "drc": drc_report,
        "antenna": antenna_report,
    }
    core = {
        "schema": SCHEMA,
        "status": "pass" if not blockers else "fail",
        "policy": {
            "negative_setup_or_hold_slack": "fail",
            "slew_capacitance_floating_overdriven_drc_antenna": "fail",
            "functional_unconstrained_endpoint": "fail",
            "physical_only_endpoint": "exact inserted antenna diode classification only",
            "high_fanout": "diagnostic; electrical consequences fail separately",
            "reviewed_sdc_must_equal_consumed_sdc": True,
        },
        "metrics": {
            "setup_slack_ns": setup_slack,
            "hold_slack_ns": hold_slack,
            "high_fanout_rows": len(fanout),
            "maximum_fanout": max((row["fanout"] for row in fanout), default=0),
        },
        "violation_counts": violation_counts,
        "blockers": blockers,
        "physical_only_endpoints": classifications,
        "functional_unconstrained_endpoints": functional_unconstrained,
        "reports": reports,
        "provenance": provenance,
    }
    digest_data = json.dumps(core, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode()
    return core | {"gate_digest": hashlib.sha256(digest_data).hexdigest()}


def write_physical_gate(path: str | Path, result: Mapping[str, Any]) -> None:
    Path(path).write_text(json.dumps(dict(result), indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")


__all__ = ["SCHEMA", "PhysicalGateError", "evaluate_physical_gate",
           "write_physical_gate"]
