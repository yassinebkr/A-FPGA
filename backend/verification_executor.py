"""Executable assertion and coverage adapters.

The policy layer deliberately only *describes* properties and coverage
requirements.  This module is the concrete (and intentionally small) bridge
to Verilator and to cocotb-coverage style functional coverage.  It has no
dependency on the chat/job runner, so callers can run it from a job worker or
from a test with a mocked ``subprocess.run``.

The adapters return plain, versioned dictionaries in addition to the typed
policy records.  Raw tool output is retained, and when an
``EvidenceRepository`` is supplied all generated files are stored as
content-addressed artifacts.  A missing Verilator/cocotb-coverage install is
reported as ``inconclusive``/``unavailable``; it is never represented as a
passing gate.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from evidence import ArtifactRef, EvidenceRepository
from verification_policy import (
    AssertionProperty,
    AssertionResult,
    CoverageRequirement,
    CoverageResult,
    assertions_from_design_spec,
    coverage_requirements_from_design_spec,
)


SCHEMA_VERSION = "verification-execution/v1"


def _version_number(text: str) -> tuple[int, int, int]:
    match = re.search(r"(?:Verilator\s+)?(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(match.group(i) or 0) for i in range(1, 4)) if match else (0, 0, 0)


@dataclass(frozen=True)
class VerilatorCapabilities:
    """Feature flags discovered from the installed binary, not guessed."""

    version: str
    version_tuple: tuple[int, int, int]
    assert_flag: bool = False
    coverage_flag: bool = False
    line_coverage: bool = False
    toggle_coverage: bool = False
    expression_coverage: bool = False
    fsm_coverage: bool = False
    annotate: bool = False
    lcov: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "version_tuple": list(self.version_tuple),
            "assert": self.assert_flag,
            "coverage": self.coverage_flag,
            "coverage_line": self.line_coverage,
            "coverage_toggle": self.toggle_coverage,
            "coverage_expr": self.expression_coverage,
            "coverage_fsm": self.fsm_coverage,
            "annotate": self.annotate,
            "lcov": self.lcov,
        }


def detect_verilator(verilator: str = "verilator", *, runner: Callable[..., Any] | None = None,
                     coverage_runner: str = "verilator_coverage") -> VerilatorCapabilities:
    """Detect flags supported by this Verilator installation.

    Verilator 5.040 is supported.  Newer flags are enabled only when they are
    advertised by ``--help``/``verilator_coverage --help`` so an older host
    cannot fail merely because it was given a modern command line.
    """
    run = runner or subprocess.run
    try:
        version_proc = run([verilator, "--version"], capture_output=True,
                           text=True, check=False)
        version = (getattr(version_proc, "stdout", "") or getattr(version_proc, "stderr", "") or "").strip()
    except (OSError, subprocess.SubprocessError):
        return VerilatorCapabilities("unavailable", (0, 0, 0))
    try:
        help_proc = run([verilator, "--help"], capture_output=True, text=True, check=False)
        help_text = (getattr(help_proc, "stdout", "") or "") + (getattr(help_proc, "stderr", "") or "")
    except (OSError, subprocess.SubprocessError):
        help_text = ""
    try:
        cov_proc = run([coverage_runner, "--help"], capture_output=True, text=True, check=False)
        cov_help = (getattr(cov_proc, "stdout", "") or "") + (getattr(cov_proc, "stderr", "") or "")
    except (OSError, subprocess.SubprocessError):
        cov_help = ""
    def has(flag: str) -> bool:
        return flag in help_text
    def cov_has(flag: str) -> bool:
        return flag in cov_help
    return VerilatorCapabilities(
        version=version or "unknown", version_tuple=_version_number(version),
        assert_flag=has("--assert"), coverage_flag=has("--coverage"),
        line_coverage=has("--coverage-line"), toggle_coverage=has("--coverage-toggle"),
        expression_coverage=has("--coverage-expr"), fsm_coverage=has("--coverage-fsm"),
        annotate=cov_has("--annotate"), lcov=cov_has("--write-info") or cov_has("lcov"),
    )


def _run_text(proc: Any) -> tuple[int, str]:
    return int(getattr(proc, "returncode", 1)), (
        (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    ).strip()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:100] or "artifact"


def _artifact(repo: EvidenceRepository | None, path: Path, *, media_type: str) -> ArtifactRef | None:
    if repo is None or not path.is_file():
        return None
    return repo.put_artifact(path.read_bytes(), name=path.name, media_type=media_type)


@dataclass
class FunctionalCoverageAdapter:
    """DesignSpec-driven sampled functional coverage.

    Requirements declare an event ``field`` (for example ``inputs.op``),
    legal ``bins`` and optional ``cross`` requirement ids.  Differential
    regression samples use the stable shape ``inputs/expected/actual`` and
    are accepted by :meth:`sample_events`.  When cocotb-coverage 2.x is
    installed this adapter creates real ``CoverPoint``/``CoverCross`` objects;
    otherwise it keeps deterministic counters but reports ``inconclusive`` so
    a fallback can never masquerade as an authoritative coverage gate.
    """

    requirements: list[CoverageRequirement]
    points: dict[str, dict[str, int]] = field(default_factory=dict)
    crosses: dict[str, dict[str, int]] = field(default_factory=dict)
    _last_values: dict[str, Any] = field(default_factory=dict, repr=False)
    _events_sampled: int = field(default=0, repr=False)
    _native_points: dict[str, Any] = field(default_factory=dict, repr=False)
    _native_crosses: dict[str, Any] = field(default_factory=dict, repr=False)
    _native_sampler_points: dict[str, Callable[[Mapping[str, Any]], Any]] = field(default_factory=dict, repr=False)
    _native_sampler_crosses: dict[str, Callable[[Mapping[str, Any]], Any]] = field(default_factory=dict, repr=False)
    _native_db: Any = field(default=None, repr=False)
    _fallback_overrides: set[str] = field(default_factory=set, repr=False)
    backend: str = field(default="fallback", init=False)

    def __post_init__(self) -> None:
        self._configure_fallback()
        self._configure_native()

    @classmethod
    def from_design_spec(cls, spec: Any, *, design_class: str | None = None) -> "FunctionalCoverageAdapter":
        return cls(coverage_requirements_from_design_spec(spec, design_class=design_class))

    @staticmethod
    def upstream_api() -> dict[str, Any]:
        """Probe the optional cocotb-coverage 2.x public API lazily."""
        try:
            from cocotb_coverage import coverage as upstream
        except ImportError:
            return {"available": False, "version": None, "symbols": []}
        symbols = [name for name in ("CoverPoint", "CoverCross", "coverage_db")
                   if hasattr(upstream, name)]
        return {"available": len(symbols) == 3,
                "version": getattr(upstream, "__version__", None),
                "symbols": symbols}

    @staticmethod
    def _lookup(event: Mapping[str, Any], path: str | None, default: Any = None) -> Any:
        if not path:
            return event
        value: Any = event
        for part in str(path).split("."):
            if isinstance(value, Mapping) and part in value:
                value = value[part]
            else:
                return default
        return value

    @staticmethod
    def _bins(req: CoverageRequirement) -> list[Any]:
        # An untyped requirement is a boolean cover check.  Typed bins are
        # deliberately explicit; no guessed bins are created from arbitrary
        # integers in a vector.
        return list(req.bins) if req.bins else [True]

    def _configure_fallback(self) -> None:
        for req in self.requirements:
            if req.cross:
                self.crosses[req.id] = {"__dimensions__": list(req.cross)}
            else:
                self.points[req.id] = {str(value): 0 for value in self._bins(req)}

    def _configure_native(self) -> None:
        try:
            from cocotb_coverage import coverage as upstream
            if not all(hasattr(upstream, item) for item in ("CoverPoint", "CoverCross", "coverage_db")):
                return
        except ImportError:
            return
        import uuid
        prefix = "verification_" + uuid.uuid4().hex[:10]
        self._native_db = upstream.coverage_db
        self.backend = "cocotb-coverage"
        # CoverCross requires point objects to exist first.  Coverpoints use
        # the same event field contract as the fallback collector.
        for req in self.requirements:
            if req.cross:
                continue
            bins = self._bins(req)
            field_path = req.field or req.target
            name = f"{prefix}.{req.id}"
            point = upstream.CoverPoint(
                name=name,
                xf=lambda event, path=field_path: self._lookup(event, path),
                bins=bins,
                bins_labels=[str(item) for item in bins],
            )
            def body(event: Mapping[str, Any]) -> None:
                return None
            self._native_points[req.id] = point
            self._native_sampler_points[req.id] = point(body)
        for req in self.requirements:
            if not req.cross:
                continue
            names = [f"{prefix}.{item}" for item in req.cross if item in self._native_points]
            if len(names) != len(req.cross):
                continue
            cross = upstream.CoverCross(name=f"{prefix}.{req.id}", items=names)
            def body(event: Mapping[str, Any]) -> None:
                return None
            self._native_crosses[req.id] = cross
            self._native_sampler_crosses[req.id] = cross(body)

    def add_point(self, requirement_id: str, bins: Iterable[Any] | Mapping[str, Any]) -> None:
        names = [str(name) for name in bins] if not isinstance(bins, Mapping) else [str(name) for name in bins]
        self._fallback_overrides.add(requirement_id)
        self.points[requirement_id] = {name: 0 for name in names}

    def add_cross(self, requirement_id: str, dimensions: Iterable[str]) -> None:
        self._fallback_overrides.add(requirement_id)
        self.crosses[requirement_id] = {"__dimensions__": [str(item) for item in dimensions]}

    def sample(self, requirement_id: str, value: Any = True, *, event: Mapping[str, Any] | None = None) -> None:
        event = event or {requirement_id: value}
        if requirement_id in self._native_sampler_points:
            self._native_sampler_points[requirement_id](event)
        if requirement_id in self._native_sampler_crosses:
            self._native_sampler_crosses[requirement_id](event)
        if requirement_id in self.points:
            point = self.points[requirement_id]
            key = str(value)
            if key in point:
                point[key] += 1
            elif "True" in point and bool(value):
                point["True"] += 1
        elif requirement_id in self.crosses:
            dimensions = self.crosses[requirement_id].get("__dimensions__", [])
            key = ":".join(str(self._lookup(event, name, value)) for name in dimensions)
            self.crosses[requirement_id].pop("__dimensions__", None)
            self.crosses[requirement_id].setdefault(key, 0)
            self.crosses[requirement_id][key] += 1
        self._last_values[requirement_id] = value

    def sample_event(self, event: Mapping[str, Any]) -> None:
        """Sample one differential event (inputs/expected/actual mapping)."""
        for req in self.requirements:
            if req.cross:
                self.sample(req.id, event=event)
                continue
            value = self._lookup(event, req.field or req.target)
            if value is None and req.id in event:
                value = event[req.id]
            if value is not None:
                self.sample(req.id, value, event=event)
        self._events_sampled += 1

    def sample_events(self, events: Iterable[Mapping[str, Any]]) -> None:
        for event in events:
            if isinstance(event, Mapping):
                self.sample_event(event)

    def _native_result(self, req: CoverageRequirement) -> CoverageResult | None:
        item = self._native_points.get(req.id) or self._native_crosses.get(req.id)
        if item is None or not getattr(item, "size", 0):
            return None
        covered = int(getattr(item, "coverage", 0))
        total = int(getattr(item, "size", 0))
        return CoverageResult.from_counts(req.id, covered, total, req.threshold, source="cocotb-coverage")

    def results(self) -> list[CoverageResult]:
        out: list[CoverageResult] = []
        for req in self.requirements:
            native = self._native_result(req) if self.backend == "cocotb-coverage" and req.id not in self._fallback_overrides else None
            if native is not None:
                out.append(native)
                continue
            bins = self.points.get(req.id) or self.crosses.get(req.id) or {}
            bins = {name: count for name, count in bins.items() if name != "__dimensions__"}
            total = len(bins)
            covered = sum(1 for count in bins.values() if count > 0)
            out.append(CoverageResult.from_counts(req.id, covered, total, req.threshold,
                                                   source="fallback"))
        return out

    def status(self) -> str:
        if self.backend != "cocotb-coverage" or self._events_sampled == 0:
            return "inconclusive"
        return "passed" if all(item.passed for item in self.results()
                               if next((req for req in self.requirements if req.id == item.requirement_id), None).mandatory) else "failed"

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": "functional-coverage/v1", "backend": self.backend,
                "events_sampled": self._events_sampled, "points": self.points,
                "crosses": self.crosses,
                "results": [item.model_dump(mode="json") for item in self.results()]}

    def export(self, directory: str | os.PathLike[str], *, repository: EvidenceRepository | None = None) -> dict[str, Any]:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        paths = [self.save(root / "functional-coverage.json")]
        if self.backend == "cocotb-coverage" and self._native_db is not None:
            xml_path, yaml_path = root / "functional-coverage.xml", root / "functional-coverage.yml"
            try:
                self._native_db.export_to_xml(filename=str(xml_path))
                paths.append(xml_path)
            except Exception:
                pass
            try:
                self._native_db.export_to_yaml(filename=str(yaml_path))
                paths.append(yaml_path)
            except Exception:
                pass
        refs = []
        for path in paths:
            ref = _artifact(repository, path, media_type="application/json" if path.suffix == ".json" else "application/xml" if path.suffix == ".xml" else "text/yaml")
            if ref:
                refs.append(ref.model_dump(mode="json"))
        return {"paths": [str(path) for path in paths], "artifacts": refs}

    def save(self, path: str | os.PathLike[str]) -> Path:
        target = Path(path)
        _write(target, json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n")
        return target


def _property_statuses(properties: Sequence[AssertionProperty], *, returncode: int, output: str,
                       tool: str) -> list[AssertionResult]:
    failed_names = set()
    for line in output.splitlines():
        for prop in properties:
            if prop.name in line and re.search(r"%Error|assert(?:ion)?\s+(?:failed|failure)|failed", line, re.I):
                failed_names.add(prop.id)
    if returncode != 0 and not failed_names:
        return [AssertionResult(property_id=p.id, status="inconclusive", tool=tool,
                                message="Verilator compilation or execution failed") for p in properties]
    return [AssertionResult(property_id=p.id,
                            status="failed" if p.id in failed_names else "proven",
                            tool=tool,
                            message="assertion failed" if p.id in failed_names else None)
            for p in properties]


@dataclass
class VerilatorAssertionExecutor:
    verilator: str = "verilator"
    runner: Callable[..., Any] = subprocess.run
    coverage_runner: str = "verilator_coverage"
    testbench_top: str = "tb"

    def execute(self, verilog_code: str, testbench_code: str, top_module: str, spec: Any,
                *, work_dir: str | os.PathLike[str] | None = None,
                repository: EvidenceRepository | None = None) -> dict[str, Any]:
        properties = assertions_from_design_spec(spec)
        root = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="verification-"))
        root.mkdir(parents=True, exist_ok=True)
        rtl = _write(root / f"{_safe_name(top_module)}.sv", verilog_code)
        tb = _write(root / "tb.sv", testbench_code)
        from verification_policy import render_sva_for_design_spec
        checker = _write(root / "agent_properties.sv", render_sva_for_design_spec(
            spec, module_name="agent_properties", bind_target=top_module))
        caps = detect_verilator(self.verilator, runner=self.runner, coverage_runner=self.coverage_runner)
        artifacts = [_artifact(repository, item, media_type="text/plain") for item in (rtl, tb, checker)]
        artifacts = [item for item in artifacts if item is not None]
        artifact_dicts = [item.model_dump(mode="json") for item in artifacts]
        if not caps.assert_flag:
            assertions = [AssertionResult(property_id=p.id, status="inconclusive", tool="verilator",
                                          message="installed Verilator does not advertise --assert")
                          for p in properties]
            return {"schema_version": SCHEMA_VERSION, "ok": False, "status": "unavailable",
                    "tool": "verilator", "tool_version": caps.version,
                    "assertions": [item.model_dump(mode="json") for item in assertions],
                    "artifacts": artifact_dicts, "capabilities": caps.as_dict(), "raw_output": ""}
        obj = root / "obj_dir"
        exe = obj / "verification_sim"
        cmd = [self.verilator, "--binary", "--timing", "--assert", "--top-module", self.testbench_top,
               "-Mdir", str(obj), "-o", exe.name, str(rtl), str(checker), str(tb)]
        try:
            compile_proc = self.runner(cmd, cwd=str(root), capture_output=True, text=True, check=False)
            compile_rc, compile_output = _run_text(compile_proc)
        except (OSError, subprocess.SubprocessError) as exc:
            compile_rc, compile_output = 127, str(exc)
        run_output = ""
        run_rc = compile_rc
        if compile_rc == 0:
            try:
                run_proc = self.runner([str(exe)], cwd=str(root), capture_output=True, text=True, check=False)
                run_rc, run_output = _run_text(run_proc)
            except (OSError, subprocess.SubprocessError) as exc:
                run_rc, run_output = 127, str(exc)
        output = "\n".join(item for item in (compile_output, run_output) if item)
        assertions = _property_statuses(properties, returncode=run_rc, output=output, tool="verilator")
        return {"schema_version": SCHEMA_VERSION, "ok": run_rc == 0 and all(x.status == "proven" for x in assertions),
                "status": "passed" if run_rc == 0 else "failed", "tool": "verilator",
                "tool_version": caps.version, "assertions": [item.model_dump(mode="json") for item in assertions],
                "artifacts": [item.model_dump(mode="json") for item in artifacts], "capabilities": caps.as_dict(),
                "command": cmd, "returncode": run_rc, "raw_output": output}


@dataclass
class VerilatorCoverageExecutor:
    verilator: str = "verilator"
    coverage_runner: str = "verilator_coverage"
    runner: Callable[..., Any] = subprocess.run
    testbench_top: str = "tb"

    def collect(self, verilog_code: str, testbench_code: str, top_module: str, *, work_dir: str | os.PathLike[str] | None = None,
                repository: EvidenceRepository | None = None) -> dict[str, Any]:
        root = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="coverage-"))
        root.mkdir(parents=True, exist_ok=True)
        rtl = _write(root / f"{_safe_name(top_module)}.sv", verilog_code)
        tb = _write(root / "tb.sv", testbench_code)
        caps = detect_verilator(self.verilator, runner=self.runner, coverage_runner=self.coverage_runner)
        if not caps.coverage_flag:
            return {"schema_version": SCHEMA_VERSION, "status": "unavailable", "ok": False,
                    "tool": "verilator", "tool_version": caps.version,
                    "capabilities": caps.as_dict(), "coverage": {}, "artifacts": []}
        flags = ["--coverage"]
        for enabled, flag in ((caps.line_coverage, "--coverage-line"), (caps.toggle_coverage, "--coverage-toggle"),
                              (caps.expression_coverage, "--coverage-expr"), (caps.fsm_coverage, "--coverage-fsm")):
            if enabled:
                flags.append(flag)
        obj = root / "obj_dir"
        exe = obj / "coverage_sim"
        compile_cmd = [self.verilator, "--binary", "--timing", "--top-module", self.testbench_top,
                       "-Mdir", str(obj), "-o", exe.name, *flags, str(rtl), str(tb)]
        try:
            proc = self.runner(compile_cmd, cwd=str(root), capture_output=True, text=True, check=False)
            compile_rc, compile_output = _run_text(proc)
        except (OSError, subprocess.SubprocessError) as exc:
            compile_rc, compile_output = 127, str(exc)
        run_output = ""
        if compile_rc == 0:
            try:
                proc = self.runner([str(exe)], cwd=str(root), capture_output=True, text=True, check=False)
                run_rc, run_output = _run_text(proc)
            except (OSError, subprocess.SubprocessError) as exc:
                run_rc, run_output = 127, str(exc)
        else:
            run_rc = compile_rc
        dat_files = sorted(root.glob("**/*.dat"))
        summary = ""
        lcov_path = root / "coverage.info"
        annotated = root / "annotated"
        coverage_cmds: list[list[str]] = []
        if dat_files:
            data = str(dat_files[0])
            try:
                proc = self.runner([self.coverage_runner, "--summary", data], cwd=str(root), capture_output=True, text=True, check=False)
                _, summary = _run_text(proc)
            except (OSError, subprocess.SubprocessError) as exc:
                summary = str(exc)
            if caps.annotate:
                annotated.mkdir(exist_ok=True)
                coverage_cmds.append([self.coverage_runner, "--annotate", str(annotated), data])
            if caps.lcov:
                coverage_cmds.append([self.coverage_runner, "--write-info", str(lcov_path), data])
            for cmd in coverage_cmds:
                try:
                    self.runner(cmd, cwd=str(root), capture_output=True, text=True, check=False)
                except (OSError, subprocess.SubprocessError):
                    pass
        report = _parse_coverage_summary(summary)
        artifacts: list[ArtifactRef] = []
        for path in dat_files:
            item = _artifact(repository, path, media_type="application/octet-stream")
            if item: artifacts.append(item)
        for path, media in ((lcov_path, "text/plain"),):
            item = _artifact(repository, path, media_type=media)
            if item: artifacts.append(item)
        raw_path = _write(root / "verilator-coverage-summary.txt", summary or "")
        item = _artifact(repository, raw_path, media_type="text/plain")
        if item: artifacts.append(item)
        if annotated.is_dir():
            for path in sorted(annotated.rglob("*")):
                if path.is_file():
                    item = _artifact(repository, path, media_type="text/plain")
                    if item: artifacts.append(item)
        return {"schema_version": SCHEMA_VERSION, "status": "passed" if run_rc == 0 and dat_files else "inconclusive",
                "ok": run_rc == 0 and bool(dat_files), "tool": "verilator", "tool_version": caps.version,
                "capabilities": caps.as_dict(), "coverage": report, "summary": summary,
                "artifacts": [item.model_dump(mode="json") for item in artifacts], "raw_output": "\n".join(x for x in (compile_output, run_output) if x),
                "commands": {"compile": compile_cmd, "postprocess": coverage_cmds}}


def _parse_coverage_summary(summary: str) -> dict[str, Any]:
    """Normalize Verilator's human summary without relying on one release's wording."""
    out: dict[str, Any] = {"line": {}, "toggle": {}, "expression": {}, "fsm": {}, "overall_percent": None}
    for line in summary.splitlines():
        match = re.search(r"(line|toggle|expression|expr|fsm|overall|total)[^\n]*?(\d+(?:\.\d+)?)\s*%", line, re.I)
        if not match:
            continue
        kind = match.group(1).lower().replace("expr", "expression")
        pct = float(match.group(2)) / 100.0
        if kind in {"overall", "total"}:
            out["overall_percent"] = pct
        else:
            out.setdefault(kind, {})["percentage"] = pct
        counts = re.search(r"(\d+)\s*/\s*(\d+)", line)
        if counts and kind not in {"overall", "total"}:
            out[kind]["covered"] = int(counts.group(1))
            out[kind]["total"] = int(counts.group(2))
    return out


def persist_execution_result(result: Mapping[str, Any], repository: EvidenceRepository) -> dict[str, Any]:
    """Persist raw JSON plus any files not already stored by an executor."""
    payload = json.dumps(result, default=lambda value: value.model_dump(mode="json") if hasattr(value, "model_dump") else str(value),
                         indent=2, sort_keys=True).encode()
    artifact = repository.put_artifact(payload, name="verification-result.json", media_type="application/json")
    out = dict(result)
    refs = list(out.get("artifacts", []))
    if not any((item.get("sha256") if isinstance(item, Mapping) else getattr(item, "sha256", None)) == artifact.sha256 for item in refs):
        refs.append(artifact)
    out["artifacts"] = refs
    return out

