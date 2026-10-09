"""EDA tools exposed to the agent.

- write_and_check_syntax: structured lint via the first available of slang /
  verilator / iverilog / yosys; returns a JSON diagnostic report (see lint.py).
- run_simulation: executes a testbench (iverilog or verilator) — verification before synthesis.
- run_silicon_compiler: staged SiliconCompiler execution targeting skywater130.
  The default ``flow_mode=auto`` preserves deepest-installed-flow behavior;
  callers may explicitly request ``screen`` (synthesis+STA) or ``pnr`` (full
  place-and-route).

run_simulation and run_silicon_compiler are async and execute their work as
JobManager jobs (see jobs.py): output streams live to subscribers, the job
survives WebSocket disconnects, and cancel_job kills the process tree. The
agent-visible result text is unchanged except for a final "job_id: <id>"
marker line the frontend parses to subscribe to the job's log.

SiliconCompiler itself runs in a dedicated subprocess (sc_job.py) — never in
a worker thread of this process: SC's scheduler forks node processes, and
forking a multithreaded asyncio process makes the children exit non-zero at
shutdown, so nodes get recorded as 'error' despite clean logs. A clean
interpreter avoids that and makes cancel a plain process-group kill.

write_and_check_syntax stays synchronous (fast path): lint is a single
sub-second subprocess, so it doesn't need a job. Both it and run_simulation
go through the tool-result cache (tool_cache.py): identical inputs return the
cached result instead of re-running the EDA tool.

Storage locations come from the environment at call time (see _scpath/_build_root):
SCPATH for SiliconCompiler's cache, SC_BUILDS for build outputs.
"""
import asyncio
import contextvars
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from collections.abc import Mapping

import jobs as jobs_mod
import gate_netlist
import lint as lint_mod
import physical_layout
import sc_job
import timing_paths
import tool_cache
import timing_diagnosis
from eda_workspace import EdaWorkspace, validate_target, validate_top_module
from constraints import ConstraintSet

logger = logging.getLogger("eda")

# Owning chat session (project id) for JobManager.submit, set by agent_core
# around each tool invocation. Jobs are tagged with it so the frontend can
# filter the global job registry per project. None outside a chat context.
current_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "eda_current_session", default=None)


def require_synthesis_allowed(verification_result) -> None:
    """Explicit synthesis/delivery guard for callers with a verification result.

    Existing ``run_silicon_compiler`` calls intentionally remain compatible
    and are not implicitly changed.  New delivery flows should call this
    helper immediately before synthesis, passing the structured result from
    ``run_verification`` (or its persisted ``RunManifest``).
    """
    from verification_pipeline import require_synthesis_allowed as _require
    _require(verification_result)


def _scpath() -> str:
    """SC's cache dir (PDK/tool downloads). Env SCPATH wins; default NOT ~/.sc —
    that dir belongs to SiliconCompiler itself (credentials + PDK cache)."""
    return os.environ.setdefault(
        "SCPATH", str(Path.home() / ".local/share/asic-agent"))


def _build_root() -> str:
    """Root directory for lint/sim/synthesis build outputs. Env SC_BUILDS wins."""
    return str(EdaWorkspace().root)


def _write_verilog(verilog_code: str, top_module: str) -> str:
    return str(EdaWorkspace().write_verilog(verilog_code, top_module))


def _run_linter(tool: str, verilog_path: str, build_dir: str,
                top_module: str) -> tuple[list[dict], int, str]:
    """Run the chosen linter and return (diagnostics, returncode, raw output).

    slang/verilator>=5.038 emit machine-readable JSON (slang --diag-json,
    verilator --diagnostics-sarif-output) into a scratch file in the build
    dir which is removed after parsing; older verilator, iverilog and yosys
    are parsed from their text output."""
    if tool == "slang":
        diag_json = os.path.join(build_dir, f"{top_module}.diag.json")
        cmd = ["slang", "--lint-only", "--diag-json", diag_json, verilog_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        raw = (proc.stdout + proc.stderr).strip()
        try:
            with open(diag_json) as f:
                diags = lint_mod.parse_slang_json(f.read())
        except OSError:
            diags = []
        finally:
            if os.path.exists(diag_json):
                os.remove(diag_json)
        return diags, proc.returncode, raw

    if tool == "verilator":
        version = lint_mod.tool_version("verilator")
        cmd = ["verilator", "--lint-only", "-Wall", "-Wno-fatal"]
        sarif_path = None
        if lint_mod.verilator_supports_sarif(version):
            sarif_path = os.path.join(build_dir, f"{top_module}.sarif")
            # NB: the space-separated form is required — Verilator rejects
            # --diagnostics-sarif-output=<file> ("Invalid option").
            cmd += ["--diagnostics-sarif-output", sarif_path]
        cmd.append(verilog_path)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        raw = (proc.stdout + proc.stderr).strip()
        if sarif_path is not None and os.path.exists(sarif_path):
            try:
                with open(sarif_path) as f:
                    diags = lint_mod.parse_verilator_sarif(f.read())
            except OSError:
                diags = []
            finally:
                if os.path.exists(sarif_path):
                    os.remove(sarif_path)
            return diags, proc.returncode, raw
        # SARIF unavailable or unsupported by this binary after all — fall
        # back to the classic text output rather than reporting nothing.
        return lint_mod.parse_verilator_text(raw), proc.returncode, raw

    if tool == "iverilog":
        cmd = ["iverilog", "-t", "null", verilog_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        raw = (proc.stdout + proc.stderr).strip()
        return lint_mod.parse_iverilog_text(raw), proc.returncode, raw

    # yosys
    cmd = ["yosys", "-p", f"read_verilog {verilog_path}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    raw = (proc.stdout + proc.stderr).strip()
    return lint_mod.parse_yosys_text(raw, default_file=verilog_path), proc.returncode, raw


def write_and_check_syntax(verilog_code: str, top_module: str, policy=None) -> str:
    """
    Writes Verilog code to the build directory and lints it with the first
    available HDL linter (slang, verilator, iverilog, yosys). Returns a
    structured JSON diagnostic report (schema documented in lint.py): verdict,
    error/warning totals, and up to 8 diagnostics with file/line/col/code and
    caret snippets. Identical inputs are served from the tool-result cache
    ("cached": true in the JSON). An optional verification-policy object
    applies stable rule ids and scoped waivers after the cached/raw report.
    """
    validate_top_module(top_module)
    workspace = EdaWorkspace()
    # Child synthesis processes change cwd. Freeze the build root to the same
    # absolute trust boundary used by this process before they are spawned.
    os.environ["SC_BUILDS"] = str(workspace.root)
    verilog_path = str(workspace.write_verilog(verilog_code, top_module))
    build_dir = os.path.dirname(verilog_path)

    tool = lint_mod.pick_linter()
    if tool is None:
        logger.warning("no HDL linter installed; syntax check skipped for %s", top_module)
        return (f"Code saved to {verilog_path}, but no HDL linter "
                "(slang/verilator/iverilog/yosys) is installed — syntax not checked.")

    version = lint_mod.tool_version(tool)
    key = tool_cache.make_key(tool, version, verilog_code)
    cached = tool_cache.get(key)
    if cached is not None:
        logger.info("lint %s: cache hit (%s %s)", top_module, tool, key[:12])
        result = json.loads(cached)
        result["cached"] = True
        if policy is not None:
            from verification_policy import apply_lint_policy
            result = apply_lint_policy(result, policy, top_module=top_module)
            result["cached"] = True
        return json.dumps(result, indent=2)

    logger.info("lint %s with %s", top_module, tool)
    diags, returncode, raw = _run_linter(tool, verilog_path, build_dir, top_module)
    # Cache the tool-native normalized report. Policy is intentionally applied
    # after lookup so two projects with different waivers never share policy
    # decisions.
    result = lint_mod.build_result(tool, version, verilog_code, verilog_path,
                                   diags, returncode, raw)
    if returncode != 0:
        logger.info("lint %s failed (%s): %s", top_module, result["verdict"], raw[:2000])
    tool_cache.put(key, json.dumps(result))
    if policy is not None:
        from verification_policy import apply_lint_policy
        result = apply_lint_policy(result, policy, top_module=top_module)
    return json.dumps(result, indent=2)


_SIM_PASS_MARKER_RE = re.compile(
    r"(?im)^\s*(?:pass(?:\b|\s+check\b)|"
    r"testbench\s+result\s*:\s*all\s+.*passed|"
    r"all\s+.*tests?\s+passed)"
)


def _simulation_verdict(returncode: int, output: str) -> tuple[str, str]:
    """Classify simulator output conservatively.

    A zero exit code only proves that the simulator terminated.  A usable
    verification checkpoint also needs an explicit positive assertion marker;
    otherwise a trace/debug bench (or an empty bench) could authorize RTL
    synthesis without checking behavior.
    """
    text = output or ""
    if returncode != 0 or "$fatal" in text or re.search(r"(?i)\bFAIL(?:ED)?\b", text):
        return "SIMULATION FAILED", "failure"
    if not _SIM_PASS_MARKER_RE.search(text):
        return "SIMULATION INCONCLUSIVE", "inconclusive"
    return "SIMULATION PASSED", "success"


async def run_simulation(verilog_code: str, testbench_code: str, top_module: str) -> str:
    """
    Writes the design and its testbench to the build directory and runs a
    functional simulation using the first available simulator (iverilog or
    verilator). Returns the simulation output. The testbench should print
    PASS/FAIL lines and finish with $finish.

    The compile + simulate steps run as a JobManager job (kind "simulation");
    the returned text ends with a "job_id: <id>" marker line. Identical
    inputs (design + testbench + simulator version) are served from the
    tool-result cache instead of re-running the simulation.
    """
    workspace = EdaWorkspace()
    validate_top_module(top_module)
    build_dir = str(workspace.design_dir(top_module))
    design_path = str(workspace.write_verilog(verilog_code, top_module))
    tb_path = str(workspace.write_testbench(testbench_code, top_module))

    timeout_msg = ("Simulation TIMED OUT (120s). Check the testbench for missing "
                   "$finish or hanging clocks.")
    if shutil.which("iverilog") and shutil.which("vvp"):
        sim_bin = os.path.join(build_dir, f"{top_module}.vvp")
        compile_cmd = ["iverilog", "-g2012", "-o", sim_bin, design_path, tb_path]
        sim_cmd = ["vvp", sim_bin]
        compile_timeout = 120
        sim_used = "iverilog"
    elif shutil.which("verilator"):
        compile_cmd = ["verilator", "--binary", "--timing", "-Wno-fatal",
                       "-o", f"{top_module}_sim", design_path, tb_path]
        sim_cmd = [os.path.join(build_dir, "obj_dir", f"{top_module}_sim")]
        compile_timeout = 600
        sim_used = "verilator"
    else:
        logger.warning("no simulator installed; simulation skipped for %s", top_module)
        return (f"Code saved to {build_dir}, but no simulator (iverilog or "
                "verilator) is installed — design not verified.")

    # Cache: identical design + testbench + simulator version -> same result.
    sim_version = lint_mod.tool_version(sim_used)
    cache_key = tool_cache.make_key(sim_used, sim_version, verilog_code, testbench_code)
    cached = tool_cache.get(cache_key)
    if cached is not None:
        logger.info("sim %s: cache hit (%s %s)", top_module, sim_used, cache_key[:12])
        return (f"{cached}\n(cached: true — identical design and testbench "
                f"already simulated with {sim_used})")



    async def runner(job):
        job.build_dir = build_dir
        try:
            rc, comp_out = await job.run_process(compile_cmd, cwd=build_dir, timeout=compile_timeout)
            if rc != 0:
                job.set_outcome("failure")
                logger.info("sim %s compile failed: %s", top_module, comp_out[-2000:])
                return f"Simulation compile FAILED ({sim_used}):\n{comp_out.strip()}"
            rc, output = await job.run_process(sim_cmd, cwd=build_dir, timeout=120)
        except jobs_mod.JobTimeoutError:
            job.set_outcome("failure")
            logger.warning("sim %s timed out", top_module)
            return timeout_msg
        logger.info("sim %s (%s) exit=%d: %s", top_module, sim_used, rc, output[-2000:])
        tail = output[-3000:] if len(output) > 3000 else output
        verdict, outcome = _simulation_verdict(rc, output)
        job.set_outcome(outcome)
        return f"{verdict} ({sim_used})\n{tail.strip()}"

    job = jobs_mod.manager.submit(
        "simulation", {"top_module": top_module}, runner=runner, design=top_module,
        session=current_session.get())
    try:
        result = await job.wait()
    except jobs_mod.JobCancelledError:
        result = "Simulation CANCELLED by user."
    else:
        tool_cache.put(cache_key, result)
    return f"{result}\njob_id: {job.id}"



def _pick_flow(project) -> str:
    """Kept for backwards compatibility; real work moved to sc_job."""
    return sc_job.pick_flow(project)


# RTL/SDC helpers live in sc_job (the synthesis subprocess); re-exported here.
_detect_clock_port = sc_job.detect_clock_port
_detect_reset_port = sc_job.detect_reset_port


def _stage_context_digest(verilog_code: str, top_module: str, target: str,
                         flow_mode: str, clock_period_ns: float | None,
                         constraints: ConstraintSet | None) -> str:
    """Return a stable identity for one explicit staged implementation.

    Including the RTL and all flow-affecting inputs prevents a later candidate
    from reusing or overwriting an earlier screen/PNR directory.  The digest
    is deliberately content-based and contains no model-controlled path data.
    """
    rtl_sha256 = hashlib.sha256(verilog_code.encode("utf-8")).hexdigest()
    payload = {
        "top_module": top_module,
        "rtl_sha256": rtl_sha256,
        "target": target,
        "flow_mode": flow_mode,
        "clock_period_ns": (float(clock_period_ns)
                             if clock_period_ns is not None else None),
        "constraints_sha256": (constraints.content_sha256()
                                if constraints is not None else None),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _coerce_constraints(value: ConstraintSet | Mapping | None) -> ConstraintSet | None:
    """Validate a typed constraint contract at the EDA boundary."""
    if value is None:
        return None
    if isinstance(value, ConstraintSet):
        return value
    if isinstance(value, Mapping):
        return ConstraintSet.model_validate(value)
    raise TypeError("constraints must be a ConstraintSet or mapping")


def _write_constraint_sdc(constraints: ConstraintSet, top_module: str,
                         build_dir: str) -> tuple[str, str]:
    """Render and hash the exact SDC that will be handed to SiliconCompiler."""
    validate_top_module(top_module)
    workspace = EdaWorkspace()
    build_dir = str(workspace.build_dir(build_dir))
    os.makedirs(build_dir, exist_ok=True)
    sdc_path = os.path.join(build_dir, f"{top_module}.sdc")
    content = constraints.to_sdc().encode("utf-8")
    with open(sdc_path, "wb") as stream:
        stream.write(content)
    import hashlib
    return sdc_path, hashlib.sha256(content).hexdigest()


def _write_sdc(top_module: str, clock_period_ns: float,
               clock_port: str | None = None,
               verilog_code: str = "",
               target: str = "skywater130_demo",
               build_dir: str | None = None) -> str:
    """Write an OpenSTA-compatible SDC under the design's build dir (or
    *build_dir* if given). See sc_job.write_sdc for the constraint details."""
    validate_top_module(top_module)
    validate_target(target)
    workspace = EdaWorkspace()
    if build_dir is None:
        build_dir = str(workspace.design_dir(top_module))
    else:
        build_dir = str(workspace.build_dir(build_dir))
    return sc_job.write_sdc(top_module, clock_period_ns, clock_port=clock_port,
                            verilog_code=verilog_code, target=target,
                            build_dir=build_dir)


# Steps ordered from most-refined (end of flow) to earliest. For each metric
# the last available value in this order wins, i.e. we report the most
# post-route number the flow produced.
_STEP_PRIORITY = (
    "write.views", "write.gds",
    "route.detailed_antenna_repair", "route.detailed", "route.antenna_repair", "route.global",
    "cts.fillcell", "cts.repair_timing", "cts.clock_tree_synthesis",
    "place.detailed", "place.repair_design", "place.global",
    "floorplan.pin_placement", "floorplan.power_grid", "floorplan.tapcell", "floorplan.init",
    "cleanup.clean", "synthesis.timing", "timing", "synthesis", "route",
)

# metric name -> unit
_METRICS = {
    "cellarea": "um^2",
    "totalarea": "um^2",
    "utilization": "%",
    "cells": "",
    "registers": "",
    "pins": "",
    "nets": "",
    "peakpower": "mw",
    "leakagepower": "mw",
    "setupslack": "ns",
    "holdslack": "ns",
    "setuptns": "ns",
    "unconstrained": "",
    "fmax": "Hz",
}


def _read_metrics(build_dir: str, top_module: str) -> dict:
    """Read metrics from the result manifest
    (<build>/<top>/job0/<top>.pkg.json, metric/<name>/node/<step>/0/value),
    taking the value from the most-refined step that produced one."""
    validate_top_module(top_module)
    build_dir = str(EdaWorkspace().build_dir(build_dir))
    manifest = os.path.join(build_dir, top_module, "job0", f"{top_module}.pkg.json")
    if not os.path.exists(manifest):
        return {}
    import json
    with open(manifest) as f:
        data = json.load(f)
    out = {}
    for name, unit in _METRICS.items():
        nodes = (data.get("metric", {}).get(name) or {}).get("node") or {}
        for step in _STEP_PRIORITY:
            try:
                value = nodes[step]["0"]["value"]
            except (KeyError, TypeError):
                continue
            if value is not None:
                out[name] = f"{value} {unit}".strip()
                break
    return out


def _read_timing_diagnosis(build_dir: str, top_module: str,
                           metrics: Mapping | None = None) -> dict:
    """Return normalized OpenSTA path/slack evidence for a completed build."""
    return timing_diagnosis.read_timing_diagnosis(build_dir, top_module, metrics)


# Standard-cell library identity per SiliconCompiler demo target. Used for
# artifact provenance only; it never influences flow behavior.
_TARGET_LIBRARIES = {
    "skywater130_demo": "sky130_fd_sc_hd",
    "freepdk45_demo": "NangateOpenCellLibrary",
    "asap7_demo": "asap7sc7p5",
    "gf180_demo": "gf180mcu_fd_sc_mcu7t5v0",
}


def _target_library(target: str) -> str | None:
    return _TARGET_LIBRARIES.get(target)


def _persist_gate_netlist(emission: Mapping, *, job_id: str | None,
                          project: str | None, top_module: str,
                          source_sha256: str | None,
                          constraints_sha256: str | None,
                          library: str | None) -> dict:
    """Build, validate, and persist the gate-netlist artifact for one job.

    Returns the manifest metadata (never the artifact itself) or an honest
    ``unavailable`` record when the subprocess emitted nothing usable.
    """
    if not isinstance(emission, Mapping) or emission.get("status") != "available":
        reason = (emission or {}).get("reason") if isinstance(emission, Mapping) else None
        return gate_netlist.unavailable(reason
                                       or "no structured synthesis output was produced")
    path = emission.get("path")
    if not isinstance(path, str) or not os.path.isfile(path):
        return gate_netlist.unavailable("structured netlist output disappeared")
    built = gate_netlist.build_job_netlist_manifest(
        path, top_module=top_module, job_id=job_id, project=project,
        source_sha256=source_sha256, constraints_sha256=constraints_sha256,
        library=library)
    artifact = built.pop("artifact", None)
    if artifact is None:
        return built  # unavailable with reason
    try:
        import evidence
        ref = evidence.repository.put_artifact(
            gate_netlist.canonical_bytes(artifact),
            name=f"{top_module}.gate-netlist.json",
            media_type="application/json",
            metadata={"kind": "gate_netlist", "schema": gate_netlist.SCHEMA_VERSION,
                      "job_id": job_id, "project": project,
                      "top_module": top_module,
                      "artifact_sha256": built.get("artifact_sha256")})
    except Exception as exc:  # evidence store failure must not lose the run
        logger.exception("gate netlist persistence failed for job %s", job_id)
        return gate_netlist.unavailable(f"artifact persistence failed: {exc}")
    built["repository_sha256"] = ref.sha256
    built["size_bytes"] = ref.size_bytes
    return built


def _persist_layout_view(build_dir: str, top_module: str, gate_meta: dict,
                         *, job_id: str | None, project: str | None,
                         clock_period_ns: float | None) -> dict:
    """Build, validate, and persist the layout view for one job.

    Only a run whose flow emitted a DEF produces a view; a screening
    synthesis reports an honest ``unavailable`` instead of a fabricated
    layout.
    """
    if not isinstance(gate_meta, Mapping) or not gate_meta.get("available"):
        return physical_layout.layout_view_unavailable(
            "no gate netlist artifact exists for this run")
    repository_sha = gate_meta.get("repository_sha256")
    if not isinstance(repository_sha, str) or not repository_sha:
        return physical_layout.layout_view_unavailable(
            "gate netlist artifact is not durably stored")
    netlist_artifact = None
    try:
        import evidence
        content = evidence.repository.get_artifact(repository_sha)
        if content is not None:
            netlist_artifact = gate_netlist.load_gate_netlist(json.loads(content))
    except Exception:
        netlist_artifact = None
    summary = physical_layout.collect_layout_view(
        build_dir, top_module, netlist_artifact, job_id=job_id,
        project=project, clock_period_ns=clock_period_ns)
    artifact_doc = summary.pop("artifact", None)
    if artifact_doc is None:
        return summary
    try:
        import evidence
        ref = evidence.repository.put_artifact(
            gate_netlist.canonical_bytes(artifact_doc),
            name=f"{top_module}.layout-view.json",
            media_type="application/json",
            metadata={"kind": "layout_view",
                      "schema": physical_layout.SCHEMA_VERSION,
                      "job_id": job_id, "project": project,
                      "top_module": top_module,
                      "netlist_sha256": summary.get("netlist_sha256")})
    except Exception as exc:  # evidence store failure must not lose the run
        logger.exception("layout view persistence failed for job %s", job_id)
        return physical_layout.layout_view_unavailable(f"artifact persistence failed: {exc}")
    summary["repository_sha256"] = ref.sha256
    summary["size_bytes"] = ref.size_bytes
    return summary


def _persist_timing_paths(build_dir: str, top_module: str, gate_meta: dict,
                          *, job_id: str | None, project: str | None,
                          clock_period_ns: float | None) -> dict:
    """Build, validate, and persist the timing-path artifact for one job.

    The artifact is bound to the exact gate-netlist artifact this job
    produced; a missing netlist artifact or missing STA reports yield an
    honest ``unavailable`` record instead of guessed paths.
    """
    if not isinstance(gate_meta, Mapping) or not gate_meta.get("available"):
        return timing_paths.timing_paths_unavailable(
            "no gate netlist artifact exists for this run")
    repository_sha = gate_meta.get("repository_sha256")
    if not isinstance(repository_sha, str) or not repository_sha:
        return timing_paths.timing_paths_unavailable(
            "gate netlist artifact is not durably stored")
    try:
        import evidence
        import gate_netlist
        content = evidence.repository.get_artifact(repository_sha)
        if content is None:
            return timing_paths.timing_paths_unavailable(
                "stored gate netlist artifact is missing")
        artifact = gate_netlist.load_gate_netlist(json.loads(content))
    except Exception as exc:
        logger.exception("timing path build could not load the netlist artifact for job %s", job_id)
        return timing_paths.timing_paths_unavailable(f"netlist artifact rejected: {exc}")
    summary = timing_paths.collect_timing_paths(
        build_dir, top_module, artifact, job_id=job_id, project=project,
        clock_period_ns=clock_period_ns)
    artifact_doc = summary.pop("artifact", None)
    if artifact_doc is None:
        return summary  # unavailable with reason
    try:
        import evidence
        ref = evidence.repository.put_artifact(
            gate_netlist.canonical_bytes(artifact_doc),
            name=f"{top_module}.timing-paths.json",
            media_type="application/json",
            metadata={"kind": "timing_paths",
                      "schema": timing_paths.SCHEMA_VERSION,
                      "job_id": job_id, "project": project,
                      "top_module": top_module,
                      "netlist_sha256": summary.get("netlist_sha256")})
    except Exception as exc:  # evidence store failure must not lose the run
        logger.exception("timing path persistence failed for job %s", job_id)
        return timing_paths.timing_paths_unavailable(f"artifact persistence failed: {exc}")
    summary["repository_sha256"] = ref.sha256
    summary["size_bytes"] = ref.size_bytes
    return summary



def _sc_job_cmd(top_module: str, verilog_path: str, build_dir: str,
                target: str, clock_period_ns: float | None,
                flow: str = "auto", constraints_path: str | None = None) -> list[str]:
    """Command line for the standalone SC runner (sc_job.py). Extracted so
    tests can substitute a fake runner."""
    validate_top_module(top_module)
    validate_target(target)
    sc_job.validate_flow_mode(flow)
    workspace = EdaWorkspace()
    workspace.build_dir(build_dir)
    # Validate the file itself as well as its cwd: a symlinked source file
    # must not let the subprocess consume an artifact outside SC_BUILDS.
    workspace.build_dir(verilog_path)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sc_job.py")
    cmd = [sys.executable, script,
           "--top", top_module, "--verilog", verilog_path,
           "--build-dir", build_dir, "--target", target, "--flow", flow]
    if clock_period_ns is not None:
        cmd += ["--clock-period-ns", str(clock_period_ns)]
    if constraints_path is not None:
        workspace.build_dir(constraints_path)
        cmd += ["--sdc", constraints_path]
    return cmd


def _parse_scjob(output: str) -> dict:
    """Extract the final 'SCJOB {...}' summary line printed by sc_job.py."""
    matches = re.findall(r"^SCJOB (\{.*\})$", output, re.MULTILINE)
    if not matches:
        return {}
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return {}


# Per-design build locks: run_silicon_compiler/find_fmax for the same
# top_module share a SiliconCompiler build dir — two concurrent runs stomp
# each other's node dirs ("outputs/<top>.pkg.json: No such file or directory"
# at random flow steps). A second call for a design that's already building
# fails fast instead. Locks are loop-bound, so rebind when the loop changes
# (tests drive one event loop per test, like JobManager._ensure_started).
_build_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
_build_jobs: dict[str, str] = {}  # design -> running job id (for the busy msg)


def _build_lock(design: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _build_locks.get(design)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _build_locks[design] = entry
    return entry[1]


def _build_busy_msg(tool: str, design: str) -> str:
    other = _build_jobs.get(design)
    msg = (f"{tool} not started: another build for design '{design}' is already "
           f"running")
    if other:
        msg += f" (job_id {other})"
    return msg + ". Wait for it to finish or cancel it first."


async def run_silicon_compiler(verilog_code: str, top_module: str, target: str = "skywater130_demo",
                               clock_period_ns: float | None = None,
                               constraints: ConstraintSet | Mapping | None = None,
                               flow: str | None = None,
                               flow_mode: str = "auto") -> str:
    """
    Runs SiliconCompiler to synthesize Verilog code.
    Returns the PPA (Power, Performance, Area) report or errors.
    If clock_period_ns is given, a <top>.sdc is generated (create_clock on the
    'clk' port) and added as the "sdc" fileset so timing is constrained and
    setup/hold slack metrics are reported. If omitted but the design declares a
    'clk' input, a 10 ns default period is applied automatically; only purely
    combinational designs (no 'clk' port) run unconstrained.
    Note: skywater130_demo is the default because freepdk45_demo's flowgraph
    has a broken route.antenna_repair input wiring with the installed
    SiliconCompiler/OpenROAD versions. ``flow_mode`` defaults to ``auto`` for
    backwards compatibility. Use ``screen`` for an explicit cheap
    synthesis+STA result or ``pnr`` for an explicit full place-and-route run;
    explicit modes fail closed when their required tools are unavailable.

    SC runs as a JobManager job (kind "synthesis") in a dedicated subprocess
    (sc_job.py — see its docstring for why SC must not run in-process); its
    console output streams into the job's live log. The returned text ends
    with a "job_id: <id>" marker line. Serialized per design via _build_lock.
    """
    validate_top_module(top_module)
    validate_target(target)
    # ``flow`` is the Phase 3 controller spelling; ``flow_mode`` is the
    # descriptive direct-Python spelling. Either is opt-in, while a call that
    # supplies neither retains the historical auto selection.
    requested_flow = flow if flow is not None else flow_mode
    flow_mode = sc_job.validate_flow_mode(requested_flow)
    lock = _build_lock(top_module)
    if lock.locked():
        return _build_busy_msg("Synthesis", top_module)
    async with lock:
        try:
            return await _run_silicon_compiler(verilog_code, top_module, target,
                                               clock_period_ns, constraints, flow_mode)
        finally:
            _build_jobs.pop(top_module, None)


async def _run_silicon_compiler(verilog_code: str, top_module: str, target: str,
                                clock_period_ns: float | None,
                                constraints: ConstraintSet | Mapping | None = None,
                                flow_mode: str = "auto") -> str:
    _scpath()  # ensure SCPATH is set before the subprocess inherits the env
    flow_mode = sc_job.validate_flow_mode(flow_mode)
    constraints = _coerce_constraints(constraints)
    if constraints is not None and clock_period_ns is not None:
        raise ValueError("pass either constraints or clock_period_ns, not both")
    try:
        import siliconcompiler  # noqa: F401 — just a presence check
    except ImportError:
        return "Synthesis unavailable: the 'siliconcompiler' package is not installed."

    # Default-constrain sequential designs: without a clock the flow logs
    # "No clocks defined" and slack/fmax metrics come back null.
    if constraints is None and clock_period_ns is None and re.search(r"\binput\b[^;()]*\bclk\b", verilog_code):
        clock_period_ns = 10.0
        logger.info("synthesis %s: no clock_period_ns given, defaulting to 10 ns "
                    "('clk' port detected)", top_module)

    context_digest = _stage_context_digest(
        verilog_code, top_module, target, flow_mode, clock_period_ns, constraints)
    rtl_sha256 = hashlib.sha256(verilog_code.encode("utf-8")).hexdigest()

    workspace = EdaWorkspace()
    # Child synthesis processes change cwd. Freeze the build root to the same
    # absolute trust boundary used by this process before they are spawned.
    os.environ["SC_BUILDS"] = str(workspace.root)
    verilog_path = str(workspace.write_verilog(verilog_code, top_module))
    # Legacy/auto runs retain the historical build path. Explicit staged runs
    # are isolated so a cheap screen can never stomp a promoted P&R run (or
    # vice versa), while both remain beneath the trusted SC_BUILDS root.
    base_design_dir = workspace.design_dir(top_module)
    build_dir = str(base_design_dir if flow_mode == "auto" else
                    workspace.build_dir(base_design_dir / flow_mode / context_digest))
    os.makedirs(build_dir, exist_ok=True)
    constraint_sdc = None
    constraint_sha256 = None
    if constraints is not None:
        constraint_sdc, constraint_sha256 = _write_constraint_sdc(constraints, top_module, build_dir)
    job0_dir = os.path.join(build_dir, top_module, "job0")

    async def runner(job):
        job.build_dir = build_dir
        job.manifest_path = os.path.join(job0_dir, f"{top_module}.pkg.json")
        rc, output = await job.run_process(
            _sc_job_cmd(top_module, verilog_path, build_dir, target, clock_period_ns,
                        flow=flow_mode, constraints_path=constraint_sdc) if constraint_sdc else
            _sc_job_cmd(top_module, verilog_path, build_dir, target, clock_period_ns,
                        flow=flow_mode),
            cwd=build_dir)
        summary = _parse_scjob(output)
        flow_desc = summary.get("flow", "unknown flow")
        effective_mode = sc_job.validate_flow_mode(summary.get("flow_mode", flow_mode))
        stage = summary.get("stage") or sc_job._flow_stage(effective_mode, flow_desc)
        artifact_manifest = (summary.get("artifacts") or {}).get("siliconcompiler_manifest")
        run_manifest = {
            "schema_version": 1,
            "status": "success" if rc == 0 and summary.get("ok", True) else "failure",
            "ok": bool(rc == 0 and summary.get("ok", True)),
            "flow_mode": effective_mode,
            "mode": effective_mode,
            "stage": stage,
            "flow": flow_desc,
            "target": target,
            "top_module": top_module,
            "context_digest": context_digest,
            "rtl_sha256": rtl_sha256,
            "manifest_path": artifact_manifest or job.manifest_path,
            "artifacts": {
                "siliconcompiler_manifest": artifact_manifest or job.manifest_path,
            },
        }
        consumed = summary.get("constraints") or {}
        if constraint_sha256 is not None and consumed.get("sha256") != constraint_sha256:
            run_manifest["status"] = "failure"
            run_manifest["ok"] = False
            job.manifest = run_manifest
            job.set_outcome("failure")
            return ("Synthesis failed: subprocess did not report the exact typed "
                    f"constraint artifact (expected sha256 {constraint_sha256}, "
                    f"got {consumed.get('sha256')!r}).")
        if rc != 0 or summary.get("ok") is False:
            run_manifest["status"] = "failure"
            run_manifest["ok"] = False
            job.manifest = run_manifest
            job.set_outcome("failure")
            tail = output[-2500:] if len(output) > 2500 else output
            logger.info("synthesis %s failed (rc=%d): %s", top_module, rc, tail[-1000:])
            return (f"Synthesis failed ({flow_desc}). Log tail:\n{tail.strip()}")

        metrics = _read_metrics(build_dir, top_module)
        diagnosis = _read_timing_diagnosis(build_dir, top_module, metrics)
        has_ppa_metrics = bool(metrics)
        if constraint_sha256 is not None:
            # Keep the semantic contract hash distinct from the exact bytes
            # consumed by the subprocess.  Both are part of the job manifest.
            metrics = {**metrics,
                       "constraints_sha256": constraints.content_sha256(),
                       "constraints_sdc_sha256": constraint_sha256,
                       "constraints_sdc_path": constraint_sdc}
        # Keep legacy metric keys at the top level for existing dashboard
        # consumers, while exposing a stable structured run manifest for
        # staged-flow decisions and evidence consumers.
        job.manifest = {**metrics, **run_manifest, "metrics": metrics}
        if diagnosis.get("status") == "available":
            job.manifest["timing_diagnosis"] = diagnosis

        logger.info("synthesis %s finished, mode=%s stage=%s metrics=%s",
                    top_module, effective_mode, stage, metrics)
        header = f"Synthesis successful ({flow_desc})!"
        if not has_ppa_metrics:
            run_manifest["status"] = "inconclusive"
            run_manifest["ok"] = False
            job.manifest.update(run_manifest)
            job.set_outcome("inconclusive")
            return f"{header}\nNo metrics were reported. Check the build log in {build_dir}"
        job.set_outcome("success")
        # Phase G1: persist the canonical gate-netlist artifact through the
        # evidence store and bind it to this job's manifest. Building and
        # validating the canonical artifact parses the whole netlist JSON, so
        # it runs off the event loop (this is plain parsing, not SC — the
        # no-fork rule only forbids SiliconCompiler in this process).
        gate_meta = await asyncio.to_thread(
            _persist_gate_netlist,
            summary.get("gate_netlist") or {},
            job_id=job.id, project=current_session.get(),
            top_module=top_module, source_sha256=rtl_sha256,
            constraints_sha256=(constraints.content_sha256() if constraints is not None else None),
            library=_target_library(target))
        job.manifest["gate_netlist"] = gate_meta
        # Phase G3: normalize the exact STA run into an immutable ordered
        # timing-path artifact mapped onto the same netlist, and persist it
        # through the evidence store. Absence is honest: a synthesis whose
        # STA step produced no ordered reports reports `unavailable`.
        timing_meta = await asyncio.to_thread(
            _persist_timing_paths, build_dir, top_module, gate_meta,
            job_id=job.id, project=current_session.get(),
            clock_period_ns=clock_period_ns)
        job.manifest["timing_paths"] = timing_meta
        # Phase G4: a post-P&R run also gets an indexed, digest-bound layout
        # view derived from the routed DEF. Screening runs report `unavailable`.
        layout_meta = await asyncio.to_thread(
            _persist_layout_view, build_dir, top_module, gate_meta,
            job_id=job.id, project=current_session.get(),
            clock_period_ns=clock_period_ns)
        job.manifest["layout_view"] = layout_meta
        lines = "\n".join(f"{k}: {v}" for k, v in metrics.items())
        if diagnosis.get("status") == "available":
            lines += "\ntiming_diagnosis: " + json.dumps(diagnosis, sort_keys=True, separators=(",", ":"))
        return f"{header}\n{lines}"

    job = jobs_mod.manager.submit(
        "synthesis",
        {"top_module": top_module, "target": target, "clock_period_ns": clock_period_ns,
         "flow_mode": flow_mode, "context_digest": context_digest},
        runner=runner, design=top_module, session=current_session.get())
    _build_jobs[top_module] = job.id
    try:
        result = await job.wait()
    except jobs_mod.JobCancelledError:
        result = "Synthesis CANCELLED by user."
    return f"{result}\njob_id: {job.id}"


def _validate_fmax_options(period_tolerance_ns: float, max_runs: int, confirm_runs: int, min_period_ns: float, max_period_ns: float) -> tuple[float, int, int, float, float]:
    """Validate bounded Fmax controls before any PDK process is started."""
    numbers = (period_tolerance_ns, min_period_ns, max_period_ns)
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in numbers):
        raise ValueError("Fmax bounds and tolerance must be finite numbers")
    if min_period_ns <= 0 or max_period_ns <= min_period_ns:
        raise ValueError("Fmax requires 0 < min_period_ns < max_period_ns")
    if period_tolerance_ns <= 0 or period_tolerance_ns >= max_period_ns - min_period_ns:
        raise ValueError("period_tolerance_ns must be positive and smaller than the bracket")
    if not isinstance(max_runs, int) or isinstance(max_runs, bool) or max_runs < 3:
        raise ValueError("max_runs must be an integer of at least 3")
    if not isinstance(confirm_runs, int) or isinstance(confirm_runs, bool) or confirm_runs < 0:
        raise ValueError("confirm_runs must be a non-negative integer")
    if max_runs < 3 + confirm_runs:
        raise ValueError("max_runs must leave room for coarse probes and confirmations")
    return (float(period_tolerance_ns), max_runs, confirm_runs, float(min_period_ns), float(max_period_ns))


async def find_fmax(verilog_code: str, top_module: str, target: str = "skywater130_demo", period_tolerance_ns: float = 0.5, max_runs: int = 12, confirm_runs: int = 1, min_period_ns: float = 5.0, max_period_ns: float = 40.0, constraints: ConstraintSet | Mapping | None = None) -> str:
    """
    Search the maximum clock frequency for a sequential design.
    Runs synthesis + STA only (no place and route). It first probes the
    configured bracket endpoints and midpoint concurrently, then applies
    bounded binary refinement and confirmation runs. It returns structured
    observations, measured WNS, feasible/infeasible bounds, uncertainty,
    assumptions, and an inconclusive outcome for unstable timing evidence. The result ends with a "job_id: <id>" marker.
    Serialized per design via _build_lock for the whole search.
    """
    validate_top_module(top_module)
    validate_target(target)
    lock = _build_lock(top_module)
    if lock.locked():
        return _build_busy_msg("find_fmax", top_module)
    async with lock:
        try:
            return await _find_fmax(verilog_code, top_module, target, period_tolerance_ns, max_runs, confirm_runs, min_period_ns, max_period_ns, constraints)
        finally:
            _build_jobs.pop(top_module, None)


async def _find_fmax(verilog_code: str, top_module: str, target: str, period_tolerance_ns: float, max_runs: int, confirm_runs: int, min_period_ns: float, max_period_ns: float, constraints: ConstraintSet | Mapping | None = None) -> str:
    _scpath()
    constraints = _coerce_constraints(constraints)
    if constraints is not None and len(constraints.clocks) != 1:
        return "find_fmax requires exactly one typed clock constraint"
    try:
        import siliconcompiler  # noqa: F401 — just a presence check
        from siliconcompiler import targets as sc_targets
    except ImportError:
        return "find_fmax unavailable: the 'siliconcompiler' package is not installed."

    target_func = getattr(sc_targets, target, None)
    if target_func is None:
        return (f"find_fmax failed: unknown target '{target}'. "
                "Available demos include freepdk45_demo, asap7_demo, skywater130_demo, gf180_demo.")

    if not re.search(r"\binput\b[^;()]*\b(?:clk|clock|clk_i|clk_in|sys_clk)\b", verilog_code, re.IGNORECASE):
        return "find_fmax requires a clocked design with a recognized clock input port."

    (period_tolerance_ns, max_runs, confirm_runs, min_period_ns, max_period_ns) = _validate_fmax_options(period_tolerance_ns, max_runs, confirm_runs, min_period_ns, max_period_ns)

    workspace = EdaWorkspace()
    # Child synthesis processes change cwd. Freeze the build root to the same
    # absolute trust boundary used by this process before they are spawned.
    os.environ["SC_BUILDS"] = str(workspace.root)
    verilog_path = str(workspace.write_verilog(verilog_code, top_module))
    # One job, one log; each candidate period gets its OWN build subdir
    # (<top>_fmax/<p>ns/) so the parallel SC runs never collide.
    fmax_root = str(workspace.fmax_dir(top_module))
    os.makedirs(fmax_root, exist_ok=True)
    probe_constraint_hashes = {}

    async def probe(job, build_dir, period):
        """One synthesis+STA run at `period` ns. Returns
        ("ok", period, wns_ns, metrics) or ("error", message)."""
        os.makedirs(build_dir, exist_ok=True)
        probe_sdc = None
        probe_sha256 = None
        if constraints is not None:
            candidate = constraints.model_copy(update={
                "clocks": [constraints.clocks[0].model_copy(update={"period_ns": period})]
            })
            probe_sdc, probe_sha256 = _write_constraint_sdc(candidate, top_module, build_dir)
            probe_constraint_hashes[round(float(period), 3)] = probe_sha256
        command = (_sc_job_cmd(top_module, verilog_path, build_dir, target, period,
                               flow="syn-sta", constraints_path=probe_sdc)
                   if probe_sdc else
                   _sc_job_cmd(top_module, verilog_path, build_dir, target, period,
                               flow="syn-sta"))
        rc, output = await job.run_process(command, cwd=build_dir)
        summary = _parse_scjob(output)
        if probe_sha256 is not None and (summary.get("constraints") or {}).get("sha256") != probe_sha256:
            return ("error", f"period={period:.3f} ns — subprocess did not consume the "
                    "exact typed constraint artifact")
        if rc != 0:
            tail = output[-2000:] if len(output) > 2000 else output
            logger.info("find_fmax %s period=%.3f failed (rc=%d)", top_module, period, rc)
            return ("error", f"period={period:.3f} ns — Synthesis failed (exit {rc}). "
                             f"Log tail:\n{tail.strip()}")
        metrics = _read_metrics(build_dir, top_module)
        wns_str = (metrics or {}).get("setupslack")
        if wns_str is None:
            return ("error", f"period={period:.3f} ns — no setupslack reported")
        try:
            wns_ns = float(wns_str.split()[0])
        except (ValueError, IndexError):
            return ("error", f"period={period:.3f} ns — malformed slack '{wns_str}'")
        if not math.isfinite(wns_ns):
            return ("error", f"period={period:.3f} ns — non-finite slack")
        return ("ok", period, wns_ns, metrics)


    async def runner(job):
        job.build_dir = fmax_root
        lower = None
        upper = None
        best_wns = -float("inf")
        best_metrics = {}
        summaries = []
        observations = []
        runs = 0
        errors = []

        async def run_candidates(candidates, label):
            nonlocal runs, lower, upper, best_wns, best_metrics
            points = sorted({round(float(p), 3) for p in candidates})
            if not points:
                return []
            point_text = [f"{p:.3f}" for p in points]
            await job.emit(f"find_fmax {label}: probing {point_text} ns in parallel")
            results = await asyncio.gather(*(probe(job, str(workspace.period_dir(top_module, p)), p) for p in points))
            runs += len(points)
            for res in results:
                if res[0] == "error":
                    errors.append(res[1])
                    summaries.append(f"{label}: {res[1]}")
                    continue
                _, p, wns, metrics = res
                feasible = wns >= 0
                if feasible:
                    if upper is None or p < upper:
                        upper = p
                        best_wns = wns
                        best_metrics = metrics
                elif lower is None or p > lower:
                    lower = p
                status = "feasible" if feasible else "infeasible"
                observations.append({"period_ns": round(p, 3), "wns_ns": round(wns, 3), "status": status, "phase": label})
                summaries.append(f"{label}: period={p:.3f} ns, WNS={wns:.3f} ns ({status})")
            return results

        await run_candidates([min_period_ns, (min_period_ns + max_period_ns) / 2.0, max_period_ns], "coarse")
        if errors:
            job.set_outcome("failure")
            return json.dumps({"status": "failure", "error": "coarse synthesis probe failed", "details": errors, "search": {"bracket_ns": [min_period_ns, max_period_ns], "probes": observations}})
        if upper is None:
            job.set_outcome("failure")
            return json.dumps({"status": "failure", "error": "no feasible clock period found", "search": {"bracket_ns": [min_period_ns, max_period_ns], "final_bounds_ns": [lower, None], "probes": observations}})

        while runs < max_runs - confirm_runs:
            if lower is not None and upper is not None and upper - lower <= period_tolerance_ns:
                break
            if lower is None:
                candidate = min_period_ns
            elif upper is None:
                candidate = max_period_ns
            else:
                candidate = round((lower + upper) / 2.0, 3)
            if (lower is not None and candidate <= lower) or (upper is not None and candidate >= upper):
                break
            before = runs
            await run_candidates([candidate], f"fine {runs - 2}")
            if runs == before or errors:
                job.set_outcome("failure")
                return json.dumps({"status": "failure", "error": "refinement synthesis probe failed", "details": errors, "search": {"bracket_ns": [min_period_ns, max_period_ns], "probes": observations}})

        candidate_period = upper
        confirmations = []
        for index in range(confirm_runs):
            if runs >= max_runs:
                break
            before = runs
            results = await run_candidates([candidate_period], f"confirm {index + 1}")
            if runs == before or not results or results[0][0] == "error":
                break
            _, period, wns, _ = results[0]
            confirmations.append({"period_ns": round(period, 3), "wns_ns": round(wns, 3), "status": "feasible" if wns >= 0 else "infeasible"})

        # Feasibility must be monotonic with period under fixed constraints.
        feasible_periods = [item["period_ns"] for item in observations if item["status"] == "feasible"]
        infeasible_periods = [item["period_ns"] for item in observations if item["status"] == "infeasible"]
        monotonicity_violations = [
            {"feasible_period_ns": feasible, "infeasible_period_ns": infeasible}
            for feasible in feasible_periods
            for infeasible in infeasible_periods
            if feasible < infeasible
        ]
        stable = (
            len(confirmations) == confirm_runs
            and all(item["status"] == "feasible" for item in confirmations)
            and not monotonicity_violations
        )
        final_lower = round(lower, 3) if lower is not None else None
        final_upper = round(upper, 3) if upper is not None else None
        period_low = final_lower if final_lower is not None else min_period_ns
        period_high = final_upper if final_upper is not None else max_period_ns
        search = {
            "method": "coarse_bracket_then_binary_refinement",
            "bracket_ns": [min_period_ns, max_period_ns],
            "final_bounds_ns": [final_lower, final_upper],
            "infeasible_bound_ns": final_lower,
            "feasible_bound_ns": final_upper,
            "uncertainty_ns": round(max(0.0, period_high - period_low), 3),
            "fmax_bounds_mhz": [round(1000.0 / period_high, 2), round(1000.0 / period_low, 2)],
            "fmax_interval_mhz": [round(1000.0 / final_upper, 2) if final_upper else None, round(1000.0 / final_lower, 2) if final_lower else None],
            "probes": runs,
            "max_runs": max_runs,
            "period_tolerance_ns": period_tolerance_ns,
            "confirmation": {"requested": confirm_runs, "completed": len(confirmations), "status": "stable" if stable else "unstable", "observations": confirmations},
            "monotonicity": {"status": "valid" if not monotonicity_violations else "violated", "violations": monotonicity_violations},
            "toolchain": {"engine": "SiliconCompiler", "flow": "synthesis + static timing", "target": target},
            "constraints": {"clock_period_ns": round(candidate_period, 3),
                           "clock_port": constraints.clocks[0].port if constraints else "auto-detected clock input",
                           "criterion": "setupslack >= 0 ns",
                           "source": "typed ConstraintSet" if constraints else "legacy clock_period_ns",
                           "contract_sha256": constraints.content_sha256() if constraints else None,
                           "sdc_sha256_by_period": probe_constraint_hashes if constraints else {}},
            "observations": observations,
            "assumptions": ["setup slack is monotonic with increasing clock period", "the true boundary is within the configured search bracket", "each result is synthesis plus static timing, not signoff STA", "the PDK and constraints are unchanged between probes"]
        }
        result = {"status": "success" if stable else "inconclusive", "fmax_mhz": round(1000.0 / candidate_period, 2), "chosen_period_ns": round(candidate_period, 3), "wns_ns": round(best_wns, 3), "search": search}
        job.manifest = {**best_metrics, "fmax_search": result}
        job.set_outcome("success" if stable else "inconclusive")
        return "\n".join([json.dumps(result), "Fmax search summaries:"] + summaries)

    job = jobs_mod.manager.submit(
        "fmax",
        {"top_module": top_module, "target": target, "bracket_ns": [min_period_ns, max_period_ns], "period_tolerance_ns": period_tolerance_ns, "max_runs": max_runs, "confirm_runs": confirm_runs, "constraints_sha256": constraints.content_sha256() if constraints else None},
        runner=runner, design=top_module, session=current_session.get())
    _build_jobs[top_module] = job.id
    try:
        result = await job.wait()
    except jobs_mod.JobCancelledError:
        return "find_fmax CANCELLED by user."
    return f"{result}\njob_id: {job.id}"


async def run_tiny_tapeout_hardening(
    wrapper_verilog: str,
    top_module: str,
    title: str | None = None,
    author: str = "ASIC Agent",
    description: str | None = None,
    clock_hz: int = 50_000_000,
) -> str:
    """Run the conservative Tiny Tapeout hardening adapter as a JobManager job.

    The adapter owns all filesystem/tool configuration; model-controlled calls
    may provide only wrapper metadata and a positive clock value.  The result
    is JSON followed by the normal ``job_id`` marker for live log subscription.
    """
    from tapeout import run_tiny_tapeout_hardening_job

    return await run_tiny_tapeout_hardening_job(
        wrapper_verilog, top_module, title=title, author=author,
        description=description, clock_hz=clock_hz,
        session=current_session.get(), manager=jobs_mod.manager)


async def run_verification(verilog_code: str, testbench_code: str, top_module: str,
                           design_spec: dict | None = None,
                           run_icarus: bool = False,
                           reference_rtl_code: str | None = None) -> str:
    """Run the unified verification gates before a caller requests synthesis.

    This is additive to the existing lint/simulation/synthesis tools: it does
    not change their behavior or silently run verification from synthesis.
    The returned JSON contains the requirement matrix, ``RunManifest``,
    ``EvidenceReport`` and an explicit ``gate.allowed`` decision, followed by
    the normal ``job_id`` marker for live JobManager logs.
    """
    from evidence import DesignSpec
    from verification_pipeline import (
        VerificationPipeline, run_verification_job,
    )

    if design_spec is None:
        spec = DesignSpec(design_id=top_module, name=top_module,
                          top_module=top_module)
    elif isinstance(design_spec, DesignSpec):
        spec = design_spec
    else:
        parse = getattr(DesignSpec, "model_validate", None)
        spec = parse(design_spec) if parse else DesignSpec.parse_obj(design_spec)
    if spec.top_module != top_module:
        raise ValueError("design_spec.top_module must match top_module")
    pipeline = VerificationPipeline(
        evidence_repository=None, manager=jobs_mod.manager,
        run_portability=run_icarus,
    )
    result = await run_verification_job(
        jobs_mod.manager, pipeline, spec, verilog_code,
        testbench_code=testbench_code, reference_rtl_code=reference_rtl_code,
        session=current_session.get(), persist=True,
    )
    return json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str) + \
        f"\njob_id: {result.job_id}"
