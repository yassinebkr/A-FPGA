"""Standalone SiliconCompiler runner, executed as a subprocess by eda_tools.

project.run() MUST NOT run inside the backend's asyncio process: SC's
scheduler forks node processes, and forking a multithreaded asyncio process
from a worker thread (asyncio.to_thread) leaves the children broken — they
exit non-zero at interpreter shutdown, so nodes get recorded as 'error'
despite clean logs ("Could not run final steps ... due to errors in:
elaborate/0"). Running SC in its own clean interpreter avoids that entirely
and makes job cancellation a plain process-group kill (jobs.run_process).

Usage:
    python sc_job.py --top T --verilog FILE --build-dir DIR --target NAME
                     [--clock-period-ns N] [--flow auto|screen|pnr|syn-sta]

SC's console output streams to stdout (captured live into the job log by the
JobManager). The last line is machine-readable:
    SCJOB {"schema_version": 1, "flow_mode": "screen", "stage":
       "synthesis_sta", "flow": "<description>", "ok": true|false}
Exit code is 0 on success, 1 on failure.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys

from eda_workspace import EdaWorkspace, validate_target, validate_top_module


# ``auto`` retains the historical behavior (choose the deepest installed
# flow). ``screen`` is an explicit cheap synthesis+STA gate and ``pnr`` is an
# explicit full place-and-route request. ``syn-sta`` is retained as the
# internal/legacy spelling used by find_fmax.
FLOW_MODE_ALIASES = {
    "auto": "auto",
    "screen": "screen",
    "screening": "screen",
    "syn-sta": "screen",
    "pnr": "pnr",
    "full": "pnr",
    "full-pnr": "pnr",
}

SYNTHESIS_STRATEGIES = {
    "DELAY0", "DELAY1", "DELAY2", "DELAY3", "DELAY4",
    "AREA0", "AREA1", "AREA2", "AREA3",
}
DEFAULT_TIMING_STRATEGY = "DELAY1"


def validate_synthesis_strategy(strategy: str) -> str:
    if strategy not in SYNTHESIS_STRATEGIES:
        available = ", ".join(sorted(SYNTHESIS_STRATEGIES))
        raise ValueError(
            f"unsupported synthesis strategy {strategy!r}; available: {available}"
        )
    return strategy


def _synthesis_flow(name: str, *, timing_np: int = 1,
                    strategy: str = DEFAULT_TIMING_STRATEGY):
    """Build a synthesis flow with an explicit, reproducible ABC policy."""
    from siliconcompiler.flows import synflow

    validate_synthesis_strategy(strategy)
    return synflow.SynthesisFlow(name=name, timing_np=timing_np)


def _apply_synthesis_strategy(project, strategy: str) -> None:
    """Apply strategy to the task schema registered by the selected flow."""
    strategy = validate_synthesis_strategy(strategy)
    task = project.get("tool", "yosys", "task", "syn_asic", field="schema")
    task.set_yosys_strategy(strategy)


def validate_flow_mode(flow_mode: str) -> str:
    """Validate and normalize an EDA flow mode at the subprocess boundary."""
    if not isinstance(flow_mode, str) or flow_mode not in FLOW_MODE_ALIASES:
        available = "auto, screen, pnr"
        raise ValueError(f"unsupported flow mode {flow_mode!r}; available: {available}")
    return FLOW_MODE_ALIASES[flow_mode]


def _flow_stage(flow_mode: str, flow_description: str = "") -> str:
    """Return the stable stage identifier used in machine-readable results."""
    mode = validate_flow_mode(flow_mode)
    description = flow_description.lower()
    if mode == "pnr" or "place & route" in description:
        return "place_route"
    if mode == "screen" or "static timing" in description:
        return "synthesis_sta"
    if "synthesis only" in description:
        return "synthesis"
    return "auto"


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_gate_netlist_verilog(build_dir: str, top_module: str) -> str | None:
    """Return the deepest emitted gate netlist, or None when none exists.

    Each SC step keeps its outputs at ``job0/<step>/0/outputs/<top>.vg``;
    lexicographically later steps are deeper in the flow, so the last one
    that actually emitted a netlist is the implemented design.
    """
    job0 = os.path.join(build_dir, top_module, "job0")
    if not os.path.isdir(job0):
        return None
    candidates = []
    try:
        steps = sorted(os.listdir(job0))
    except OSError:
        return None
    for step in steps:
        path = os.path.join(job0, step, "0", "outputs", f"{top_module}.vg")
        if os.path.isfile(path):
            candidates.append(path)
    return candidates[-1] if candidates else None


def find_liberty_in_manifest(manifest_path: str, limit: int = 4) -> list[str]:
    """Collect real .lib files referenced by the SC manifest (bounded walk).

    Liberty files give Yosys the standard-cell port directions, so the
    structured netlist can distinguish drivers from loads. A missing library
    is not fatal: the artifact then reports unknown directions honestly.
    """
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    found: list[str] = []

    def walk(node) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, str):
            if node.endswith(".lib") and os.path.isfile(node) and node not in found:
                found.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return found


def find_liberty(build_dir: str, top_module: str,
                 manifest_path: str | None, limit: int = 4) -> list[str]:
    """Find the liberty views the flow consumed, manifest first.

    The SC manifest carries the synthesis inputs for some flows; for others
    the copied liberty files live directly under
    ``job0/<step>/0/inputs/*.lib``. Both are real flow inputs — nothing is
    guessed. Physical-only pins (VPWR/VGND) exist in the routed netlist but
    not in the liberty view; ``hierarchy`` is therefore run WITHOUT ``-check``
    in :func:`emit_gate_netlist_json` so those connections are ignored
    instead of aborting the structured emission.
    """
    found = find_liberty_in_manifest(manifest_path, limit=limit) if manifest_path else []
    if len(found) < limit:
        job0 = os.path.join(build_dir, top_module, "job0")
        if os.path.isdir(job0):
            for step in sorted(os.listdir(job0)):
                inputs = os.path.join(job0, step, "0", "inputs")
                if not os.path.isdir(inputs):
                    continue
                for name in sorted(os.listdir(inputs)):
                    if name.endswith(".lib"):
                        path = os.path.join(inputs, name)
                        if os.path.isfile(path) and path not in found:
                            found.append(path)
                            if len(found) >= limit:
                                return found
    return found


def emit_gate_netlist_json(build_dir: str, top_module: str,
                           manifest_path: str | None) -> dict:
    """Best-effort structured netlist emission for a successful synthesis.

    Runs Yosys over the emitted gate netlist to produce ``write_json`` output
    inside the job's build directory. Never raises: an emission failure is
    reported as an honest ``unavailable`` record so the synthesis result
    itself is unaffected. Yosys JSON (not reparsed Verilog) is the canonical
    source for the gate-netlist artifact (Phase G1).
    """
    import subprocess

    netlist_vg = find_gate_netlist_verilog(build_dir, top_module)
    if netlist_vg is None:
        return {"status": "unavailable",
                "reason": "the flow emitted no gate netlist"}
    if shutil.which("yosys") is None:
        return {"status": "unavailable",
                "reason": "yosys is not installed; no structured netlist output"}
    out_path = os.path.join(build_dir, top_module, "job0",
                            f"{top_module}.gate-netlist.yosys.json")
    script_parts = []
    for lib in find_liberty(build_dir, top_module, manifest_path):
        script_parts.append(f"read_liberty -lib {lib}")
    script_parts.append(f"read_verilog -sv {netlist_vg}")
    # No -check: post-P&R netlists connect physical-only pins (VPWR/VGND)
    # that the liberty view does not define; those connections are ignored
    # rather than aborting the structured emission.
    script_parts.append(f"hierarchy -top {top_module}")
    script_parts.append(f"write_json {out_path}")
    try:
        proc = subprocess.run(
            ["yosys", "-q", "-p", "; ".join(script_parts)],
            capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable",
                "reason": f"structured netlist emission failed: {exc}"}
    if proc.returncode != 0 or not os.path.isfile(out_path):
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        return {"status": "unavailable",
                "reason": "structured netlist emission failed", "detail": tail}
    return {"status": "available", "path": out_path,
            "sha256": _file_sha256(out_path)}


def detect_clock_port(verilog_code: str, top_module: str = "") -> str:
    """Return the name of the top module's clock input port if one is declared.
    Defaults to 'clk' for typical designs; otherwise scans the top module's port
    list for common clock-port names, avoiding submodules."""
    port_list = ""
    module_text = ""
    if top_module:
        m = re.search(
            rf"module\s+{re.escape(top_module)}\s*(?:#\s*\(.*?\)\s*)?\((.*?)\)\s*;(.*?)endmodule",
            verilog_code, re.DOTALL | re.MULTILINE)
        if m:
            port_list = m.group(1)
            module_text = m.group(0)
    search_text = module_text or verilog_code
    for name in ("clk", "clock", "clk_i", "clk_in", "sys_clk"):
        declared = re.search(rf"\binput\b[^;()]*\b{name}\b", search_text)
        listed = not port_list or re.search(rf"\b{name}\b", port_list)
        if declared and listed:
            return name
    m = re.search(r"\binput\b\s+(?:wire|reg|logic)?\s*(?:\[[^\]]+\]\s*)?([A-Za-z_]\w*)",
                  search_text)
    return m.group(1) if m else "clk"


def detect_reset_port(verilog_code: str, clock_port: str) -> str | None:
    """Return the async reset port name if the top module has an always block
    with a sensitivity list like @(posedge clk or negedge rst_n)."""
    pattern = (
        r"(?:always|always_ff|always_latch)\s*@\s*\(\s*"
        r"posedge\s+" + re.escape(clock_port) +
        r"\s*(?:or|\,)\s*(?:negedge|posedge)\s+([A-Za-z_]\w*)"
    )
    m = re.search(pattern, verilog_code, re.IGNORECASE)
    return m.group(1) if m else None


def write_sdc(top_module: str, clock_period_ns: float,
              clock_port: str | None = None,
              verilog_code: str = "",
              target: str = "skywater130_demo",
              build_dir: str | None = None) -> str:
    """Write an OpenSTA-compatible SDC constraints file. If clock_port is
    omitted, it is auto-detected from the RTL. The SDC includes clock
    uncertainty, I/O delays, output load, and (for sky130 targets) a default
    input buffer driving cell. Async resets are given a false path."""
    validate_top_module(top_module)
    validate_target(target)
    if build_dir is None:
        build_dir = os.getcwd()
    os.makedirs(build_dir, exist_ok=True)
    sdc_path = os.path.join(build_dir, f"{top_module}.sdc")
    if clock_port is None:
        clock_port = detect_clock_port(verilog_code, top_module)
    io_delay = clock_period_ns * 0.2
    lines = [
        f"create_clock -name {clock_port} -period {clock_period_ns} [get_ports {clock_port}]",
        f"set_clock_uncertainty 0.25 [get_clocks {clock_port}]",
        f"set_input_delay {io_delay:.3f} -clock {clock_port} [all_inputs]",
        f"set_output_delay {io_delay:.3f} -clock {clock_port} [all_outputs]",
    ]
    if target.startswith("skywater130"):
        lines.append(
            "set_driving_cell -lib_cell sky130_fd_sc_hd__buf_4 [all_inputs]"
        )
    lines.append("set_load 0.03 [all_outputs]")
    reset_port = detect_reset_port(verilog_code, clock_port)
    if reset_port:
        lines.append(f"set_false_path -from [get_ports {reset_port}]")
    with open(sdc_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return sdc_path


def pick_flow(project, flow_mode: str = "auto",
              synthesis_strategy: str = DEFAULT_TIMING_STRATEGY) -> str:
    """Choose an EDA flow and set it on the project.

    ``screen`` and ``pnr`` are explicit and never silently downgrade to a
    different stage. ``auto`` retains the historical deepest-flow behavior.
    """
    requested = validate_flow_mode(flow_mode)
    has = shutil.which
    if requested == "screen":
        if not has("yosys") or not has("sta"):
            raise RuntimeError(
                "screen flow requires both yosys and sta (OpenSTA); refusing "
                "to fall back to synthesis-only"
            )
        project.set_flow(_synthesis_flow(
            "synflow-sta", strategy=synthesis_strategy))
        return "screening synthesis + static timing"
    if requested == "pnr":
        missing = [tool for tool in ("yosys", "openroad", "klayout") if not has(tool)]
        if missing:
            raise RuntimeError(
                "pnr flow requires yosys, openroad, and klayout "
                f"(missing: {', '.join(missing)}); refusing to fall back to screening"
            )
        return "full ASIC flow (synthesis + place & route)"
    if has("openroad") and has("klayout"):
        return "full ASIC flow (synthesis + place & route)"  # target default already set
    if has("yosys") and has("sta"):
        project.set_flow(_synthesis_flow(
            "synflow-sta", strategy=synthesis_strategy))
        return "synthesis + static timing (openroad not installed)"
    if has("yosys"):
        project.set_flow(_synthesis_flow(
            "synflow-only", timing_np=0, strategy=synthesis_strategy))
        return "synthesis only (opensta/openroad not installed)"
    raise RuntimeError(
        "No EDA tools found. Install at least yosys "
        "(e.g. 'sudo apt install yosys' or the OSS CAD Suite) and make sure it is on PATH."
    )


def build_project(top_module: str, verilog_path: str, target: str,
                  clock_period_ns: float | None, flow: str,
                  build_dir: str, sdc_path: str | None = None,
                  synthesis_strategy: str = DEFAULT_TIMING_STRATEGY):
    """Construct the ASIC project (design + filesets + target + flow).
    Returns (project, flow_description)."""
    validate_top_module(top_module)
    validate_target(target)
    from siliconcompiler import ASIC, Design
    from siliconcompiler import targets as sc_targets

    target_func = getattr(sc_targets, target, None)
    if target_func is None:
        raise RuntimeError(
            f"unknown target '{target}'. Available demos include "
            "freepdk45_demo, asap7_demo, skywater130_demo, gf180_demo.")

    verilog_code = open(verilog_path).read()

    design = Design(top_module)
    with design.active_fileset("rtl"):
        design.set_topmodule(top_module)
        # Durable evidence artifacts are content-addressed by digest and
        # deliberately have no filename extension. Do not rely on extension
        # inference here: an extensionless Verilog artifact otherwise reaches
        # Yosys as an empty source set ("no input files").
        design.add_file(verilog_path, filetype="verilog")
    if sdc_path is not None:
        # An explicitly rendered SDC is authoritative; never regenerate a
        # legacy clock constraint and silently discard expert constraints.
        sdc_path = os.path.abspath(sdc_path)
        EdaWorkspace().build_dir(sdc_path)
        if not os.path.isfile(sdc_path):
            raise ValueError(f"constraint file does not exist: {sdc_path}")
    elif clock_period_ns is not None:
        # Same "sdc" fileset pattern as siliconcompiler.demos.asic_demo.
        sdc_path = write_sdc(top_module, clock_period_ns,
                             verilog_code=verilog_code, target=target,
                             build_dir=build_dir)
    if sdc_path is not None:
        with design.active_fileset("sdc"):
            design.add_file(sdc_path)

    project = ASIC(design)
    project.add_fileset("rtl")
    if sdc_path is not None:
        project.add_fileset("sdc")
    target_func(project)

    requested = validate_flow_mode(flow)
    synthesis_strategy = validate_synthesis_strategy(synthesis_strategy)
    if flow == "syn-sta":
        project.set_flow(_synthesis_flow(
            "synflow-sta", strategy=synthesis_strategy))
        flow_desc = "synthesis + static timing (find_fmax)"
    else:
        flow_desc = pick_flow(project, requested, synthesis_strategy)

    _apply_synthesis_strategy(project, synthesis_strategy)

    project.option.set_builddir(build_dir)
    project.option.set_quiet(False)  # stdout streams live into the job log
    return project, flow_desc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", required=True)
    ap.add_argument("--verilog", required=True)
    ap.add_argument("--build-dir", required=True)
    ap.add_argument("--target", default="skywater130_demo")
    ap.add_argument("--clock-period-ns", type=float, default=None)
    ap.add_argument("--sdc", default=None,
                    help="authoritative SDC file (must be under the build root)")
    ap.add_argument("--flow", choices=sorted(FLOW_MODE_ALIASES), default="auto")
    ap.add_argument("--synthesis-strategy", choices=sorted(SYNTHESIS_STRATEGIES),
                    default=DEFAULT_TIMING_STRATEGY)
    args = ap.parse_args()

    # The standalone runner is also an input boundary.  eda_tools normally
    # supplies contained paths, but reject direct CLI invocations that would
    # write outside the configured build root.
    try:
        validate_top_module(args.top)
        validate_target(args.target)
        EdaWorkspace().build_dir(args.build_dir)
        if args.sdc:
            EdaWorkspace().build_dir(args.sdc)
            if not os.path.isfile(args.sdc):
                raise ValueError(f"constraint file does not exist: {args.sdc}")
    except ValueError as exc:
        print(f"SCJOB-ERROR: {exc}", flush=True)
        print("SCJOB " + json.dumps({"schema_version": 1, "flow_mode": args.flow,
                                      "stage": "unknown", "flow": "unknown",
                                      "ok": False}), flush=True)
        return 1

    ok = False
    flow_desc = "unknown"
    effective_mode = validate_flow_mode(args.flow)
    try:
        project, flow_desc = build_project(args.top, args.verilog, args.target,
                                           args.clock_period_ns, args.flow,
                                           args.build_dir, args.sdc,
                                           args.synthesis_strategy)
        project.run()
        ok = True
    except Exception as e:
        print(f"SCJOB-ERROR: {e}", flush=True)
    manifest_path = EdaWorkspace().build_dir(
        os.path.join(args.build_dir, args.top, "job0", f"{args.top}.pkg.json")
    )
    summary = {
        "schema_version": 1,
        "flow_mode": effective_mode,
        "mode": effective_mode,
        "stage": _flow_stage(effective_mode, flow_desc),
        "flow": flow_desc,
        "ok": ok,
        "target": args.target,
        "top_module": args.top,
        "synthesis_strategy": args.synthesis_strategy,
        "manifest_path": str(manifest_path),
        "artifacts": {"siliconcompiler_manifest": str(manifest_path)},
    }
    if args.sdc and os.path.isfile(args.sdc):
        summary["constraints"] = {"format": "sdc", "path": os.path.abspath(args.sdc),
                                   "sha256": _file_sha256(args.sdc)}
    # Phase G1: a successful synthesis also emits a structured netlist (Yosys
    # JSON). Emission is best-effort and reported honestly; it never turns a
    # successful synthesis into a failure or vice versa.
    if ok:
        summary["gate_netlist"] = emit_gate_netlist_json(
            args.build_dir, args.top,
            str(manifest_path) if os.path.isfile(manifest_path) else None)
    print("SCJOB " + json.dumps(summary), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
