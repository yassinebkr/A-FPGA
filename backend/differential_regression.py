"""Cocotb/Verilator differential regression for generated RTL.

The reference corpus in :mod:`golden_models` is deliberately independent of
RTL.  This module is the bridge from that corpus to a generated top-level
module.  A run is an ordinary :class:`jobs.Job` and therefore survives a
websocket disconnect, streams the simulator output, and leaves a durable log
and machine-readable manifest under ``SC_BUILDS``.

The backend process never imports cocotb while handling a request.  Instead,
it starts this file in a clean subprocess through ``Job.run_process``.  The
worker imports cocotb and uses its public ``cocotb_tools.runner`` API (with a legacy fallback).  This keeps
the long-running simulator and cocotb scheduler out of the asyncio process
and makes the feature optional for development installations.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

import jobs as jobs_mod
from eda_workspace import EdaWorkspace, validate_top_module
from golden_models import RegressionCase, RegressionCorpus, get_reference_model

logger = logging.getLogger("eda.differential")

REPORT_MARKER = "DIFF_REPORT "
SUPPORTED_MODELS = frozenset(("alu8", "counter8", "arbiter4"))


@dataclass(frozen=True)
class DutInterface:
    """Port contract used by the generated cocotb test.

    Values are assigned as integers.  ``input_encodings`` is useful for
    symbolic inputs such as the ALU operation.  ``clock`` is optional: the
    seed blocks are combinational, but the contract supports synchronous
    generated designs by waiting for a clock edge when one is supplied.
    """

    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    input_encodings: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    clock: str | None = None
    settle_ns: int = 1
    clock_cycles: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "input_encodings": {
                key: dict(value) for key, value in self.input_encodings.items()
            },
            "clock": self.clock,
            "settle_ns": self.settle_ns,
            "clock_cycles": self.clock_cycles,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "DutInterface":
        return cls(
            tuple(str(item) for item in payload.get("inputs", ())),
            tuple(str(item) for item in payload.get("outputs", ())),
            {str(k): {str(n): int(v) for n, v in value.items()}
             for k, value in payload.get("input_encodings", {}).items()},
            payload.get("clock"),
            int(payload.get("settle_ns", 1)),
            int(payload.get("clock_cycles", 1)),
        )


def interface_for_model(model: str) -> DutInterface:
    """Return the stable port contract for a seeded golden model."""

    if model == "alu8":
        return DutInterface(
            ("a", "b", "op"), ("y", "carry", "zero"),
            {"op": {"add": 0, "sub": 1, "and": 2, "or": 3,
                     "xor": 4, "not": 5}},
        )
    if model == "counter8":
        return DutInterface(("value", "enable", "reset"), ("value", "wrap"))
    if model == "arbiter4":
        return DutInterface(("request",), ("grant", "valid"))
    raise KeyError(f"no default RTL interface for {model}")


@dataclass(frozen=True)
class DifferentialRunConfig:
    top_module: str
    verilog_code: str
    model: str
    corpus_root: str | Path
    case_id: str | None = None
    interface: DutInterface | None = None
    run_icarus: bool = False
    timeout_s: float = 900.0
    artifact_root: str | Path | None = None
    session: str | None = None


def _json_dump(path: Path, value: Any) -> None:
    """Write a small run artifact atomically, including a useful newline."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _rtl_sha256(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _tool_version(binary: str) -> str | None:
    """Best-effort version lookup used in the immutable run manifest."""

    path = shutil.which(binary)
    if not path:
        return None
    try:
        # This is a tiny metadata probe, performed before the Job starts.
        import subprocess
        completed = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (completed.stdout or completed.stderr).strip().splitlines()
    return text[0][:240] if text else None


def _reference_preflight(case: RegressionCase) -> str | None:
    """Validate stored expected values before compiling any RTL."""

    model = get_reference_model(case.model)
    if not model.provenance.independent_of_rtl:
        return "reference model is not marked independent of RTL"
    if case.model_version != model.provenance.version:
        return (f"case expects reference version {case.model_version}, "
                f"current is {model.provenance.version}")
    for vector in case.vectors:
        try:
            current = dict(model.evaluate(vector.inputs))
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        if current != dict(vector.expected):
            return f"stored expected output changed for {vector.vector_id}"
    return None


def _select_case(config: DifferentialRunConfig) -> RegressionCase:
    corpus = RegressionCorpus(config.corpus_root)
    cases = [case for case in corpus.cases() if case.model == config.model]
    if config.case_id is not None:
        cases = [case for case in cases if case.case_id == config.case_id]
    if not cases:
        wanted = config.case_id or f"a case for model {config.model}"
        raise ValueError(f"regression corpus has no {wanted}")
    if len(cases) > 1 and config.case_id is None:
        # Running an arbitrary filesystem order is not reproducible.  The
        # seeded case is the canonical default; callers can select a bug case.
        seeded = [case for case in cases if case.source == "seeded"]
        cases = seeded or cases
    return sorted(cases, key=lambda item: item.case_id)[0]


def _feature_report(config: DifferentialRunConfig, *, run_dir: Path,
                    case: RegressionCase, status: str, reason: str | None = None,
                    job_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "differential-regression/v1",
        "status": status,
        "model": config.model,
        "case_id": case.case_id,
        "vectors_total": len(case.vectors),
        "vectors_run": 0,
        "mismatches": [],
        "samples": [],
        "reason": reason,
        "rtl_sha256": _rtl_sha256(config.verilog_code),
        "top_module": config.top_module,
        "simulator": "verilator",
        "tool_versions": {
            "verilator": _tool_version("verilator"),
            "cocotb": _cocotb_version(),
            "iverilog": _tool_version("iverilog"),
        },
        "artifacts": {
            "run_dir": str(run_dir),
            "rtl": str(run_dir / f"{config.top_module}.v"),
            "case": str(run_dir / "case.json"),
            "test": str(run_dir / "test_differential.py"),
            "manifest": str(run_dir / "manifest.json"),
            "worker_report": str(run_dir / "worker-report.json"),
            "job_log": None,
        },
        "job_id": job_id,
    }


def _cocotb_version() -> str | None:
    try:
        import cocotb  # type: ignore
        return str(getattr(cocotb, "__version__", "unknown"))
    except Exception:
        return None


def _cocotb_test_source() -> str:
    """Return the deliberately boring test body written into each run dir."""

    return r'''"""Generated by differential_regression; do not edit."""
import json
import os
import cocotb
from cocotb.triggers import RisingEdge, Timer


def _as_int(value):
    # Integer conversion rejects X/Z values rather than silently turning them
    # into a passing zero.  That is a DUT/runtime failure, not a mismatch.
    return int(value)


@cocotb.test()
async def differential_test(dut):
    with open(os.environ["DIFF_CASE_PATH"], encoding="utf-8") as stream:
        case = json.load(stream)
    with open(os.environ["DIFF_INTERFACE_PATH"], encoding="utf-8") as stream:
        interface = json.load(stream)
    report_path = os.environ["DIFF_REPORT_PATH"]
    report = {"status": "passed", "vectors_run": 0, "mismatches": [],
              "samples": [], "reason": None}
    try:
        encodings = interface.get("input_encodings", {})
        for vector in case["vectors"]:
            for name in interface["inputs"]:
                value = vector["inputs"][name]
                if name in encodings:
                    value = encodings[name][str(value)]
                getattr(dut, name).value = value
            clock = interface.get("clock")
            if clock:
                for _ in range(max(1, int(interface.get("clock_cycles", 1)))):
                    await RisingEdge(getattr(dut, clock))
            else:
                # Advance one simulator step rather than a physical-time
                # delay.  ``step`` is precision-independent and works under
                # Icarus even when the RTL has no `timescale declaration`.
                await Timer(1, unit="step")
            actual = {name: _as_int(getattr(dut, name).value)
                      for name in interface["outputs"]}
            expected = vector["expected"]
            report["vectors_run"] += 1
            # Stable sampled event contract consumed by functional coverage.
            report["samples"].append({"vector_id": vector["vector_id"],
                                     "inputs": vector["inputs"],
                                     "expected": expected, "actual": actual,
                                     "passed": actual == expected})
            if actual != expected:
                report["status"] = "mismatch"
                report["mismatches"].append({
                    "vector_id": vector["vector_id"],
                    "inputs": vector["inputs"],
                    "expected": expected,
                    "actual": actual,
                    "kind": "output_mismatch",
                })
        with open(report_path, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except Exception as exc:
        report["status"] = "runtime_failure"
        report["reason"] = f"{type(exc).__name__}: {exc}"
        with open(report_path, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
        raise
'''


def _worker_report(path: Path, *, status: str, reason: str | None = None,
                   vectors_run: int = 0, mismatches: list | None = None,
                   samples: list | None = None) -> None:
    _json_dump(path, {
        "status": status,
        "vectors_run": vectors_run,
        "mismatches": mismatches or [],
        "samples": samples or [],
        "reason": reason,
    })


def _get_cocotb_runner():
    """Load the public runner API across supported cocotb releases.

    Cocotb 2.x publishes the runner under ``cocotb_tools.runner``.  The
    legacy ``cocotb.runner`` location is retained as a compatibility fallback
    for cocotb 1.x installations used by older developer environments.
    """

    try:
        from cocotb_tools.runner import get_runner  # type: ignore
        return get_runner
    except (ImportError, ModuleNotFoundError):
        from cocotb.runner import get_runner  # type: ignore
        return get_runner


def _run_cocotb(simulator: str, *, run_dir: Path, top_module: str,
                report_path: Path) -> dict[str, Any]:
    """Worker-only cocotb invocation.  Exceptions are classified by phase."""

    # The generated test reads this per-invocation path.  In particular, the
    # optional Icarus run must not overwrite the primary Verilator report.
    report_path = report_path.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DIFF_REPORT_PATH"] = str(report_path)

    try:
        get_runner = _get_cocotb_runner()
    except Exception as exc:
        return {"status": "inconclusive", "reason": f"cocotb unavailable: {exc}"}
    try:
        runner = get_runner(simulator)
        build_dir = run_dir / simulator
        build_dir.mkdir(parents=True, exist_ok=True)
        runner.build(
            sources=[str(run_dir / f"{top_module}.v")],
            hdl_toplevel=top_module,
            build_dir=str(build_dir),
            always=True,
        )
    except Exception as exc:
        _worker_report(report_path, status="compile_failure",
                       reason=f"{type(exc).__name__}: {exc}")
        return json.loads(report_path.read_text())
    try:
        old_pythonpath = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = str(run_dir) + (os.pathsep + old_pythonpath if old_pythonpath else "")
        runner.test(
            hdl_toplevel=top_module,
            test_module="test_differential",
            build_dir=str(build_dir),
            test_dir=str(run_dir),
        )
    except Exception as exc:
        if report_path.exists():
            report = json.loads(report_path.read_text())
            if report.get("status") == "mismatch":
                return report
        _worker_report(report_path, status="runtime_failure",
                       reason=f"{type(exc).__name__}: {exc}")
    if report_path.exists():
        return json.loads(report_path.read_text())
    _worker_report(report_path, status="runtime_failure",
                   reason="cocotb completed without a differential report")
    return json.loads(report_path.read_text())


def _worker_main(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    report_path = Path(args.report).resolve()
    os.environ.update({
        "DIFF_CASE_PATH": str(run_dir / "case.json"),
        "DIFF_INTERFACE_PATH": str(run_dir / "interface.json"),
        "DIFF_REPORT_PATH": str(report_path),
    })
    primary = _run_cocotb("verilator", run_dir=run_dir,
                          top_module=args.top_module, report_path=report_path)
    result = {"primary": primary, "icarus": None}
    if args.icarus == "1" and shutil.which("iverilog") and shutil.which("vvp"):
        smoke_path = run_dir / "icarus-report.json"
        result["icarus"] = _run_cocotb("icarus", run_dir=run_dir,
                                       top_module=args.top_module,
                                       report_path=smoke_path)
    elif args.icarus == "1":
        result["icarus"] = {"status": "skipped", "reason": "iverilog/vvp unavailable"}
    _json_dump(report_path, result)
    # Worker exit status is reserved for infrastructure failure.  A mismatch
    # or compile error is a valid verification result and is in the manifest.
    return 0


async def run_differential_regression_report(
    config: DifferentialRunConfig, *, manager: jobs_mod.JobManager | None = None,
) -> dict[str, Any]:
    """Run one persisted corpus case against generated RTL and return a report."""

    validate_top_module(config.top_module)
    if config.model not in SUPPORTED_MODELS:
        # get_reference_model gives the canonical error for future models too.
        get_reference_model(config.model)
    case = _select_case(config)
    workspace = EdaWorkspace()
    run_id = uuid.uuid4().hex
    if config.artifact_root is None:
        run_dir = workspace.design_dir(config.top_module) / "differential" / run_id
        workspace.build_dir(run_dir)
    else:
        run_dir = Path(config.artifact_root).expanduser().resolve()
        workspace.build_dir(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    rtl_path = run_dir / f"{config.top_module}.v"
    case_path = run_dir / "case.json"
    interface = config.interface or interface_for_model(config.model)
    rtl_path.write_text(config.verilog_code, encoding="utf-8")
    _json_dump(case_path, {
        "case_id": case.case_id, "model": case.model,
        "vectors": [{"vector_id": v.vector_id, "inputs": dict(v.inputs),
                     "expected": dict(v.expected)} for v in case.vectors],
    })
    _json_dump(run_dir / "interface.json", interface.to_json())
    (run_dir / "test_differential.py").write_text(_cocotb_test_source(), encoding="utf-8")

    base = _feature_report(config, run_dir=run_dir, case=case, status="queued")
    reference_error = _reference_preflight(case)
    if reference_error:
        base.update(status="reference_failure", reason=reference_error)
        _json_dump(run_dir / "manifest.json", base)
        return base
    if not shutil.which("verilator") or _cocotb_version() is None:
        base.update(status="inconclusive",
                    reason="Verilator and cocotb are required for RTL differential regression")
        _json_dump(run_dir / "manifest.json", base)
        return base

    manager = manager or jobs_mod.manager
    python = sys.executable

    async def runner(job: jobs_mod.Job) -> str:
        job.build_dir = str(run_dir)
        cmd = [python, str(Path(__file__).resolve()), "--worker",
               "--run-dir", str(run_dir), "--report", str(run_dir / "worker-report.json"),
               "--top-module", config.top_module,
               "--icarus", "1" if config.run_icarus else "0"]
        rc, output = await job.run_process(cmd, cwd=str(run_dir), timeout=config.timeout_s)
        if rc != 0:
            job.set_outcome("failure")
            return json.dumps({"status": "worker_failure", "reason": output[-2000:]})
        worker_path = run_dir / "worker-report.json"
        if not worker_path.exists():
            job.set_outcome("inconclusive")
            return json.dumps({"status": "inconclusive", "reason": "worker report missing"})
        report = json.loads(worker_path.read_text(encoding="utf-8"))
        primary = report.get("primary", {})
        # Job status subscribers receive this immediately at finalization;
        # the durable manifest below adds the complete artifact metadata.
        job.manifest = {
            "schema_version": "differential-regression/v1",
            "status": primary.get("status", "inconclusive"),
            "model": config.model,
            "case_id": case.case_id,
            "vectors_run": primary.get("vectors_run", 0),
            "mismatches": primary.get("mismatches", []),
            "samples": primary.get("samples", []),
            "reason": primary.get("reason"),
            "rtl_sha256": _rtl_sha256(config.verilog_code),
            "run_dir": str(run_dir),
            "job_log": job.log_path,
        }
        status = primary.get("status", "inconclusive")
        if status == "passed":
            icarus = report.get("icarus")
            if icarus and icarus.get("status") in {"compile_failure", "runtime_failure", "mismatch"}:
                status = "portability_failure"
            job.set_outcome("success" if status == "passed" else "failure")
        elif status == "inconclusive":
            job.set_outcome("inconclusive")
        else:
            job.set_outcome("failure")
        return json.dumps(report, sort_keys=True)

    job = manager.submit(
        "differential", {"top_module": config.top_module, "model": config.model,
                          "case_id": case.case_id}, runner=runner,
        design=config.top_module,
        session=config.session,
    )
    try:
        result_text = await job.wait()
    except jobs_mod.JobCancelledError:
        base.update(status="cancelled", reason="cancelled by user", job_id=job.id)
        _json_dump(run_dir / "manifest.json", base)
        return base
    try:
        worker = json.loads(result_text)
    except json.JSONDecodeError:
        worker = {"primary": {"status": "worker_failure", "reason": result_text[-2000:]}}
    primary = worker.get("primary", {})
    final_status = primary.get("status", "inconclusive")
    if final_status == "passed" and worker.get("icarus", {}).get("status") in {
        "compile_failure", "runtime_failure", "mismatch"
    }:
        final_status = "portability_failure"
    base.update(
        status=final_status,
        vectors_run=primary.get("vectors_run", 0),
        mismatches=primary.get("mismatches", []),
        samples=primary.get("samples", []),
        reason=primary.get("reason"),
        portability=worker.get("icarus"),
        job_id=job.id,
    )
    base["artifacts"]["job_log"] = job.log_path
    _json_dump(run_dir / "manifest.json", base)
    return base


async def run_differential_regression(
    verilog_code: str, top_module: str, model: str, *,
    corpus_root: str | Path = Path(__file__).with_name("regression_corpus"),
    case_id: str | None = None, run_icarus: bool = False,
    interface: DutInterface | None = None,
    manager: jobs_mod.JobManager | None = None,
    session: str | None = None,
) -> str:
    """Agent-facing wrapper returning a structured JSON result plus job id."""

    report = await run_differential_regression_report(
        DifferentialRunConfig(top_module, verilog_code, model, corpus_root,
                              case_id=case_id, interface=interface,
                              run_icarus=run_icarus, session=session),
        manager=manager,
    )
    return json.dumps(report, indent=2, sort_keys=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--run-dir", default=".")
    parser.add_argument("--report", default="worker-report.json")
    parser.add_argument("--top-module", default="dut")
    parser.add_argument("--icarus", choices=("0", "1"), default="0")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if not args.worker:
        raise SystemExit("This module is a backend API; use --worker only via JobManager")
    raise SystemExit(_worker_main(args))
