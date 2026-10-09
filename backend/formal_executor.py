"""Formal property and RTL-equivalence execution.

This module is intentionally an execution boundary rather than part of the
verification pipeline.  A property run is useful on its own (for example in
an agent repair loop), but it must never turn a missing formal tool into a
green result.  Results use the four states used by the sign-off policy:
``proven``, ``failed``, ``bounded`` and ``inconclusive``.

The implementation writes a complete, reviewable work directory (RTL,
checker, SBY/Yosys script, raw output and a JSON result), and optionally puts
each file in :class:`evidence.EvidenceRepository` as a content-addressed
artifact.  The asynchronous entry point accepts a JobManager ``Job`` so
long-running SBY/Yosys processes retain the normal process-group cancellation
and live-log behaviour.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from evidence import EvidenceRepository, repository as default_repository
from eda_workspace import validate_top_module
from verification_policy import AssertionProperty, assertions_from_design_spec, render_sva_for_design_spec


SCHEMA_VERSION = "formal-execution/v1"


class FormalStatus(StrEnum):
    PROVEN = "proven"
    FAILED = "failed"
    BOUNDED = "bounded"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class FormalProperty:
    """A reusable property independent of a particular EDA invocation."""

    id: str
    name: str
    expression: str
    kind: str = "assert"
    clock: str | None = None
    disable_iff: str | None = None
    bound: int | None = None
    requirement_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FormalPropertyResult:
    property_id: str
    status: FormalStatus
    attempts: int = 1
    message: str | None = None
    counterexample_artifact: str | None = None
    tool: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass(frozen=True)
class EquivalenceResult:
    status: FormalStatus
    reference_top: str
    candidate_top: str
    tool: str | None = None
    tool_version: str | None = None
    message: str | None = None
    counterexample_artifact: str | None = None
    artifacts: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["artifacts"] = list(self.artifacts)
        return data


def _version(binary: str, runner: Callable[..., Any] = subprocess.run) -> str:
    try:
        proc = runner([binary, "--version"], capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    text = (getattr(proc, "stdout", "") or getattr(proc, "stderr", "") or "").strip()
    return text.splitlines()[0][:240] if text else "unknown"


def detect_formal_tools(*, sby: str = "sby", yosys: str = "yosys", eqy: str = "eqy",
                        runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Return executable availability and versions without guessing support."""
    result: dict[str, Any] = {}
    for name, binary in (("sby", sby), ("yosys", yosys), ("eqy", eqy)):
        path = shutil.which(binary)
        result[name] = {"available": path is not None,
                        "path": path,
                        "version": _version(path, runner) if path else "unavailable"}
    return result


# These templates are deliberately small and explicit.  They are offered to
# callers as starting points; they are not silently attached to arbitrary RTL.
_TEMPLATES: dict[str, tuple[FormalProperty, ...]] = {
    "counter": (
        FormalProperty("counter-reset", "counter_reset", "reset |=> value == 0", clock="clk"),
        FormalProperty("counter-hold", "counter_hold", "!reset && !enable |=> value == $past(value)", clock="clk"),
        FormalProperty("counter-step", "counter_step", "!reset && enable && $past(value) != '1", clock="clk"),
    ),
    "counter8": (
        FormalProperty("counter8-reset", "counter8_reset", "reset |=> value == 0", clock="clk"),
        FormalProperty("counter8-hold", "counter8_hold", "!reset && !enable |=> value == $past(value)", clock="clk"),
        FormalProperty("counter8-wrap", "counter8_wrap", "wrap |-> ($past(value) == 8'hff && $past(enable))", clock="clk"),
    ),
    "arbiter": (
        FormalProperty("arbiter-onehot", "arbiter_onehot", "grant == 0 || $onehot(grant)"),
        FormalProperty("arbiter-valid", "arbiter_valid", "valid == (grant != 0)"),
        FormalProperty("arbiter-grant-request", "arbiter_grant_request", "(grant & ~request) == 0"),
    ),
    "arbiter4": (
        FormalProperty("arbiter4-onehot", "arbiter4_onehot", "grant == 0 || $onehot(grant)"),
        FormalProperty("arbiter4-valid", "arbiter4_valid", "valid == (grant != 0)"),
        FormalProperty("arbiter4-grant-request", "arbiter4_grant_request", "(grant & ~request) == 0"),
    ),
    "alu": (
        FormalProperty("alu-zero", "alu_zero", "zero == (y == 0)"),
        FormalProperty("alu-carry-width", "alu_carry_width", "carry inside {1'b0, 1'b1}"),
    ),
    "alu8": (
        FormalProperty("alu8-zero", "alu8_zero", "zero == (y == 0)"),
        FormalProperty("alu8-carry-width", "alu8_carry_width", "carry inside {1'b0, 1'b1}"),
    ),
}


def property_templates(kind: str) -> tuple[FormalProperty, ...]:
    """Return immutable reusable properties for a known reference design."""
    key = str(kind).strip().lower()
    return tuple(_TEMPLATES.get(key, ()))


def templates_for_design(spec_or_kind: Any) -> tuple[FormalProperty, ...]:
    """Select templates from a DesignSpec-like value without modifying it."""
    if isinstance(spec_or_kind, str):
        return property_templates(spec_or_kind)
    data = _as_mapping(spec_or_kind)
    verification = data.get("verification", {})
    verification_class = verification.get("design_class") if isinstance(verification, Mapping) else None
    values = [data.get("design_class"), verification_class, data.get("top_module"), data.get("name"), data.get("design_id")]
    for value in values:
        if value:
            found = property_templates(str(value))
            if found:
                return found
    return ()


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if dump:
        return dump(mode="python")
    return {}


def formal_properties_from_design_spec(spec: Any, *, include_templates: bool = False) -> list[FormalProperty]:
    """Normalize DesignSpec assertions and optionally append named templates."""
    result: list[FormalProperty] = []
    for item in assertions_from_design_spec(spec):
        result.append(FormalProperty(item.id, item.name, item.expression, item.kind,
                                     item.clock, item.disable_iff, None, item.requirement_id))
    if include_templates:
        existing = {item.id for item in result}
        result.extend(item for item in templates_for_design(spec) if item.id not in existing)
    return result


def render_property_checker(properties: Iterable[FormalProperty], *, top_module: str,
                            spec: Any | None = None, module_name: str = "formal_checker") -> str:
    """Render a checker with a stable bind target for SBY and review artifacts."""
    props = list(properties)
    explicit = bool(props)
    if spec is not None and not explicit:
        props = formal_properties_from_design_spec(spec)
    # Reuse the audited renderer, converting to its policy model only where
    # needed.  It supplies interface widths and safe bind connections.
    # The DesignSpec renderer is authoritative only when the caller did not provide explicit properties.
    if spec is not None and not explicit:
        try:
            return render_sva_for_design_spec(spec, module_name=module_name, bind_target=top_module)
        except Exception:
            pass
    from verification_policy import render_sva
    policy_props = [AssertionProperty(id=p.id, name=p.name, expression=p.expression,
                                       kind=p.kind, clock=p.clock, disable_iff=p.disable_iff,
                                       requirement_id=p.requirement_id)
                    for p in props]
    return render_sva(policy_props, module_name=module_name, bind_target=top_module)



@dataclass(frozen=True)
class _HarnessPort:
    name: str
    direction: str
    width: str = "1"


def _port_width(value: Any) -> str:
    if isinstance(value, int):
        if value < 1:
            raise ValueError("interface width must be positive")
        return "1" if value == 1 else f"[{value - 1}:0]"
    text = str(value or "1").strip()
    if text == "1":
        return text
    if not (text.startswith("[") and text.endswith("]")):
        raise ValueError("interface width must be an integer or packed range")
    return text


def _rtl_ports(verilog_code: str, top_module: str) -> list[_HarnessPort] | None:
    """Best-effort ANSI/non-ANSI port extraction for a DesignSpec-less run."""
    header = re.search(
        rf"\bmodule\s+{re.escape(top_module)}\b(?:\s*#\s*\(.*?\))?"
        rf"\s*(?:\((?P<ports>.*?)\))?\s*;",
        verilog_code, re.S,
    )
    if not header:
        return None
    body_start = header.end()
    end = re.search(r"\bendmodule\b", verilog_code[body_start:], re.I)
    body = verilog_code[body_start:body_start + end.start()] if end else verilog_code[body_start:]
    text = (header.group("ports") or "") + ";\n" + body
    pattern = re.compile(
        r"\b(?P<direction>input|output|inout)\b"
        r"(?:\s+(?:wire|logic|reg|signed|unsigned))*"
        r"\s*(?P<width>\[[^\]]+\])?\s*"
        r"(?P<names>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)"
        r"(?=\s*(?:,?\s*(?:input|output|inout)\b|\)|;|$))",
        re.I,
    )
    ports: list[_HarnessPort] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        direction = match.group("direction").lower()
        width = match.group("width") or "1"
        for raw_name in match.group("names").split(","):
            name = raw_name.strip()
            if not name or name.lower() in {"input", "output", "inout"} or name in seen:
                continue
            seen.add(name)
            ports.append(_HarnessPort(name, direction, width))
    return ports


def _spec_ports(spec: Any, verilog_code: str, top_module: str) -> list[_HarnessPort] | None:
    data = _as_mapping(spec) if spec is not None else {}
    raw_interfaces = data.get("interfaces", []) if isinstance(data, Mapping) else []
    rtl_ports = _rtl_ports(verilog_code, top_module)
    if rtl_ports is None:
        return None
    ports: list[_HarnessPort] = []
    seen: set[str] = set()
    if raw_interfaces:
        if not isinstance(raw_interfaces, Iterable) or isinstance(raw_interfaces, (str, bytes)):
            return None
        for raw in raw_interfaces:
            item = _as_mapping(raw)
            name = str(item.get("name", ""))
            direction = str(item.get("direction", "")).lower()
            if not name or direction not in {"input", "output", "inout"}:
                return None
            if name in seen:
                return None
            seen.add(name)
            ports.append(_HarnessPort(name, direction, _port_width(item.get("width", 1))))
        # A DesignSpec interface list is authoritative: silently dropping an
        # RTL port would make the wrapper prove a different design.
        if {port.name for port in ports} != {port.name for port in rtl_ports}:
            return None
        rtl_by_name = {port.name: port for port in rtl_ports}
        if any(port.direction != rtl_by_name[port.name].direction for port in ports):
            return None
    else:
        ports.extend(rtl_ports)
        seen.update(port.name for port in ports)
    domains = data.get("clock_domains", []) if isinstance(data, Mapping) else []
    if domains:
        if not isinstance(domains, Iterable) or isinstance(domains, (str, bytes)):
            return None
        for raw in domains:
            item = _as_mapping(raw)
            for field_name in ("clock_port", "reset_port"):
                name = item.get(field_name)
                if not name:
                    continue
                name = str(name)
                existing = next((port for port in ports if port.name == name), None)
                if existing is not None:
                    if existing.direction != "input":
                        return None
                    continue
                # A clock-domain declaration cannot invent a port absent from
                # the RTL interface; that would leave the DUT unconnected.
                return None
    return ports


def _connected_harness(verilog_code: str, top_module: str,
                       properties: Sequence[FormalProperty], spec: Any | None) -> tuple[str, str]:
    """Return renamed DUT RTL plus a connected, Yosys-elaborated wrapper.

    A ``bind`` checker is intentionally not used: the Yosys frontend accepts
    the syntax but does not reliably elaborate it into the selected top.
    """
    ports = _spec_ports(spec, verilog_code, top_module)
    if ports is None:
        raise ValueError("top-level ports cannot be derived")
    if any(port.direction == "inout" for port in ports):
        raise ValueError("inout ports are unsupported by the formal harness")
    names = {port.name: port for port in ports}
    allowed_words = {
        "assert", "assume", "cover", "property", "disable", "iff", "posedge", "negedge",
        "and", "or", "not", "until", "throughout", "within", "s_until", "strong", "weak",
        "first_match", "true", "false", "inside", "past", "rose", "fell", "stable", "changed",
        "onehot", "isunknown",
    }
    for prop in properties:
        if prop.clock and (prop.clock not in names or names[prop.clock].direction != "input"):
            raise ValueError(f"property clock is not an input port: {prop.clock}")
        expression_text = re.sub(r"\d+'[sS]?[bodhBODH][0-9a-fA-F_xzXZ]+", " ", prop.expression)
        expression_text += " " + (prop.disable_iff or "")
        unknown = sorted({item for item in re.findall(r"[A-Za-z_]\w*", expression_text)
                          if item not in names and item.lower() not in allowed_words})
        if unknown:
            raise ValueError("property references non-port signal(s): " + ", ".join(unknown))
    renamed, count = re.subn(
        rf"(\bmodule\s+){re.escape(top_module)}(\b)",
        rf"\1{top_module}_dut\2", verilog_code, count=1,
    )
    if count != 1:
        raise ValueError("top module declaration cannot be renamed")
    lines = ["// Connected formal harness; DUT inputs are unconstrained anyseq values.", f"module {top_module};"]
    for port in ports:
        declaration = f" {port.width} " if port.width != "1" else " "
        if port.direction == "input":
            lines.append(f"  (* anyseq *) logic{declaration}{port.name};")
        else:
            lines.append(f"  wire{declaration}{port.name};")
    lines.append(f"  {top_module}_dut dut (")
    lines.append(",\n".join(f"    .{port.name}({port.name})" for port in ports))
    lines.append("  );")
    for prop in properties:
        if prop.kind not in {"assert", "assume", "cover"}:
            raise ValueError(f"unsupported formal property kind: {prop.kind}")
        prefix = f"@(posedge {prop.clock}) " if prop.clock else ""
        disable = f"disable iff ({prop.disable_iff}) " if prop.disable_iff else ""
        temporal = any(token in prop.expression for token in ("|->", "|=>", "$past", "$rose", "$fell", "throughout", "until"))
        if prefix or disable or temporal:
            lines.append(f"  {prop.kind} property ({prefix}{disable}{prop.expression});")
        else:
            lines.append(f"  always @* {prop.kind}({prop.expression});")
    lines.extend(["endmodule", ""])
    return renamed, "\n".join(lines)

def _safe_write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _artifact(repo: EvidenceRepository | None, path: Path, media_type: str) -> dict[str, Any] | None:
    if repo is None or not path.is_file():
        return None
    return repo.put_artifact(path.read_bytes(), name=path.name, media_type=media_type).model_dump(mode="json")


def _status_from_output(output: str, returncode: int, *, bounded_mode: bool = False) -> FormalStatus:
    """Parse only strong tool markers; uncertain output stays inconclusive."""
    text = output or ""
    if re.search(r"(?:FAIL|FAILED|counterexample|assert(?:ion)?\s+failed|equiv_status.*failed)", text, re.I):
        return FormalStatus.FAILED
    if re.search(r"(?:reached\s+(?:the\s+)?bound|bounded|incomplete(?:\s+proof)?)", text, re.I):
        return FormalStatus.BOUNDED
    if returncode != 0:
        return FormalStatus.INCONCLUSIVE
    if bounded_mode and re.search(r"(?:PASS|PROVEN|DONE\s*\(PASS|status\s*[:=]\s*passed)", text, re.I):
        return FormalStatus.BOUNDED
    if re.search(r"(?:PROVEN|PASS|DONE\s*\(PASS|equivalence\s+(?:successfully\s+)?proven|all\s+.*equivalent)", text, re.I):
        return FormalStatus.PROVEN
    return FormalStatus.INCONCLUSIVE


def _counterexample_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in ("**/*.vcd", "**/*.fst", "**/*.yw", "**/*.wit", "**/*counterexample*", "**/*trace*"):
        files.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted(set(files))


@dataclass
class FormalExecutor:
    sby: str = "sby"
    yosys: str = "yosys"
    eqy: str = "eqy"
    runner: Callable[..., Any] = subprocess.run
    repository: EvidenceRepository | None = default_repository
    default_depth: int = 20

    def _root(self, work_dir: str | os.PathLike[str] | None, prefix: str) -> Path:
        root = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix=prefix))
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _persist_common(self, root: Path, rtl: str, checker: str | None,
                        repo: EvidenceRepository | None) -> tuple[list[dict[str, Any]], Path, Path | None]:
        rtl_path = _safe_write(root / "dut.sv", rtl)
        checker_path = _safe_write(root / "formal_checker.sv", checker) if checker is not None else None
        refs = []
        item = _artifact(repo, rtl_path, "text/x-systemverilog")
        if item: refs.append(item)
        if checker_path:
            item = _artifact(repo, checker_path, "text/x-systemverilog")
            if item: refs.append(item)
        return refs, rtl_path, checker_path

    def execute(self, verilog_code: str, top_module: str, spec: Any | None = None, *,
                properties: Sequence[FormalProperty] | None = None,
                work_dir: str | os.PathLike[str] | None = None,
                repository: EvidenceRepository | None = None,
                depth: int | None = None, mode: str = "prove") -> dict[str, Any]:
        """Run SBY synchronously and return a durable, machine-readable result."""
        validate_top_module(top_module)
        if mode not in {"prove", "bmc", "cover"}:
            raise ValueError("formal mode must be prove, bmc, or cover")
        repo = repository if repository is not None else self.repository
        props = list(properties if properties is not None else formal_properties_from_design_spec(spec or {}))
        root = self._root(work_dir, "formal-")
        checker_props = [] if properties is None and spec is not None else props
        standalone_checker = render_property_checker(checker_props, top_module=top_module, spec=spec) if props else None
        formal_rtl, harness_text, connection_error = verilog_code, None, None
        if props:
            try:
                formal_rtl, harness_text = _connected_harness(verilog_code, top_module, props, spec)
            except ValueError as exc:
                connection_error = str(exc)
        refs, _, _ = self._persist_common(root, formal_rtl, standalone_checker, repo)
        harness = None
        if harness_text is not None:
            harness = _safe_write(root / "formal_harness.sv", harness_text)
            item = _artifact(repo, harness, "text/x-systemverilog")
            if item: refs.append(item)
        checker = root / "formal_checker.sv"
        config = "\n".join(("[options]", f"mode {mode}", f"depth {int(depth or self.default_depth)}", "", "[engines]", "smtbmc z3", "", "[script]", f"read_verilog -formal -sv dut.sv{(' formal_harness.sv' if harness else '')}", f"prep -top {top_module}", "", "[files]", "dut.sv" + ("\nformal_harness.sv" if harness else ""), ""))
        config_path = _safe_write(root / "formal.sby", config)
        item = _artifact(repo, config_path, "text/plain")
        if item: refs.append(item)
        tools = detect_formal_tools(sby=self.sby, yosys=self.yosys, eqy=self.eqy, runner=self.runner)
        properties_out: list[FormalPropertyResult]
        output = ""
        returncode = 127
        command = [self.sby, "-f", str(config_path)]
        if not props:
            status = FormalStatus.INCONCLUSIVE
            message = "no formal properties were supplied"
        elif connection_error:
            status = FormalStatus.INCONCLUSIVE
            message = connection_error
        elif not tools["sby"]["available"]:
            status = FormalStatus.INCONCLUSIVE
            message = "SymbiYosys (sby) is not installed"
        else:
            try:
                proc = self.runner(command, cwd=str(root), capture_output=True, text=True, check=False)
                returncode, output = int(getattr(proc, "returncode", 1)), (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
            except (OSError, subprocess.SubprocessError) as exc:
                output, returncode = str(exc), 127
            status = _status_from_output(output, returncode, bounded_mode=mode == "bmc")
            if status == FormalStatus.INCONCLUSIVE and returncode == 0 and mode == "bmc":
                status = FormalStatus.BOUNDED
        cex_refs: list[dict[str, Any]] = []
        cex = _counterexample_files(root) if status == FormalStatus.FAILED else []
        for path in cex:
            item = _artifact(repo, path, "application/octet-stream")
            if item: cex_refs.append(item)
        raw_path = _safe_write(root / "formal-output.log", output)
        item = _artifact(repo, raw_path, "text/plain")
        if item: refs.append(item)
        properties_out = [FormalPropertyResult(p.id, status, message=("counterexample found" if status == FormalStatus.FAILED else message if 'message' in locals() else None), tool="sby", counterexample_artifact=(cex_refs[0]["sha256"] if cex_refs else None)) for p in props]
        result = {"schema_version": SCHEMA_VERSION, "ok": status == FormalStatus.PROVEN,
                  "status": status.value, "tool": "sby", "tool_version": tools["sby"]["version"],
                  "returncode": returncode, "mode": mode, "depth": int(depth or self.default_depth),
                  "properties": [p.as_dict() for p in props],
                  "results": [p.as_dict() for p in properties_out],
                  "artifacts": refs + cex_refs, "counterexamples": cex_refs,
                  "command": command, "raw_output": output}
        result_path = _safe_write(root / "formal-result.json", json.dumps(result, indent=2, sort_keys=True) + "\n")
        item = _artifact(repo, result_path, "application/json")
        if item: result["artifacts"].append(item)
        return result

    async def execute_async(self, verilog_code: str, top_module: str, spec: Any | None = None, *,
                            properties: Sequence[FormalProperty] | None = None,
                            work_dir: str | os.PathLike[str] | None = None,
                            repository: EvidenceRepository | None = None,
                            depth: int | None = None, mode: str = "prove", job: Any | None = None) -> dict[str, Any]:
        """Async equivalent; use ``job.run_process`` when supplied."""
        if job is None:
            return await asyncio.to_thread(self.execute, verilog_code, top_module, spec,
                                           properties=properties, work_dir=work_dir,
                                           repository=repository, depth=depth, mode=mode)
        validate_top_module(top_module)
        if mode not in {"prove", "bmc", "cover"}:
            raise ValueError("formal mode must be prove, bmc, or cover")
        repo = repository if repository is not None else self.repository
        props = list(properties if properties is not None else formal_properties_from_design_spec(spec or {}))
        root = self._root(work_dir, "formal-")
        checker_props = [] if properties is None and spec is not None else props
        standalone_checker = render_property_checker(checker_props, top_module=top_module, spec=spec) if props else None
        formal_rtl, harness_text, connection_error = verilog_code, None, None
        if props:
            try:
                formal_rtl, harness_text = _connected_harness(verilog_code, top_module, props, spec)
            except ValueError as exc:
                connection_error = str(exc)
        refs, _, _ = self._persist_common(root, formal_rtl, standalone_checker, repo)
        harness = None
        if harness_text is not None:
            harness = _safe_write(root / "formal_harness.sv", harness_text)
            item = _artifact(repo, harness, "text/x-systemverilog")
            if item: refs.append(item)
        config = "\n".join(("[options]", f"mode {mode}", f"depth {int(depth or self.default_depth)}", "", "[engines]", "smtbmc z3", "", "[script]", f"read_verilog -formal -sv dut.sv{(' formal_harness.sv' if harness else '')}", f"prep -top {top_module}", "", "[files]", "dut.sv" + ("\nformal_harness.sv" if harness else ""), ""))
        config_path = _safe_write(root / "formal.sby", config)
        tools = detect_formal_tools(sby=self.sby, yosys=self.yosys, eqy=self.eqy)
        item = _artifact(repo, config_path, "text/plain")
        if item: refs.append(item)
        if not props or connection_error:
            reason = connection_error or "no formal properties were supplied"
            result_items = [FormalPropertyResult(p.id, FormalStatus.INCONCLUSIVE, message=reason, tool="sby").as_dict() for p in props]
            result = {"schema_version": SCHEMA_VERSION, "ok": False, "status": "inconclusive", "tool": "sby", "tool_version": tools["sby"]["version"], "results": result_items, "artifacts": refs, "message": reason}
            result_path = _safe_write(root / "formal-result.json", json.dumps(result, indent=2, sort_keys=True) + "\n")
            item = _artifact(repo, result_path, "application/json")
            if item: result["artifacts"].append(item)
            return result
        if not tools["sby"]["available"]:
            return {"schema_version": SCHEMA_VERSION, "ok": False, "status": "inconclusive", "tool": "sby", "tool_version": "unavailable", "results": [FormalPropertyResult(p.id, FormalStatus.INCONCLUSIVE, message="SymbiYosys (sby) is not installed", tool="sby").as_dict() for p in props], "artifacts": refs}
        command = [self.sby, "-f", str(config_path)]
        returncode, output = await job.run_process(command, cwd=str(root))
        status = _status_from_output(output, returncode, bounded_mode=mode == "bmc")
        if status == FormalStatus.INCONCLUSIVE and returncode == 0 and mode == "bmc":
            status = FormalStatus.BOUNDED
        cex_refs = []
        for path in (_counterexample_files(root) if status == FormalStatus.FAILED else []):
            item = _artifact(repo, path, "application/octet-stream")
            if item: cex_refs.append(item)
        raw_path = _safe_write(root / "formal-output.log", output)
        item = _artifact(repo, raw_path, "text/plain")
        if item: refs.append(item)
        result = {"schema_version": SCHEMA_VERSION, "ok": status == FormalStatus.PROVEN, "status": status.value, "tool": "sby", "tool_version": tools["sby"]["version"], "returncode": returncode, "mode": mode, "results": [FormalPropertyResult(p.id, status, message="counterexample found" if status == FormalStatus.FAILED else None, tool="sby", counterexample_artifact=(cex_refs[0]["sha256"] if cex_refs else None)).as_dict() for p in props], "artifacts": refs + cex_refs, "counterexamples": cex_refs, "command": command, "raw_output": output}
        result_path = _safe_write(root / "formal-result.json", json.dumps(result, indent=2, sort_keys=True) + "\n")
        item = _artifact(repo, result_path, "application/json")
        if item: result["artifacts"].append(item)
        return result

    def equivalence(self, reference_code: str, candidate_code: str, top_module: str, *,
                    work_dir: str | os.PathLike[str] | None = None,
                    repository: EvidenceRepository | None = None) -> dict[str, Any]:
        """Compare two RTL revisions with EQY, or Yosys equiv_* when EQY is absent."""
        validate_top_module(top_module)
        repo = repository if repository is not None else self.repository
        root = self._root(work_dir, "equiv-")
        ref = _safe_write(root / "reference.sv", reference_code)
        cand = _safe_write(root / "candidate.sv", candidate_code)
        refs = [item for item in (_artifact(repo, ref, "text/x-systemverilog"), _artifact(repo, cand, "text/x-systemverilog")) if item]
        tools = detect_formal_tools(sby=self.sby, yosys=self.yosys, eqy=self.eqy, runner=self.runner)
        if tools["eqy"]["available"]:
            tool, command = "eqy", [self.eqy, "-f", str(root / "equivalence.eqy")]
            eqy = "[gold]\nread_verilog -formal -sv reference.sv\nprep -top %s\n[gate]\nread_verilog -formal -sv candidate.sv\nprep -top %s\n" % (top_module, top_module)
            cfg = _safe_write(root / "equivalence.eqy", eqy)
            item = _artifact(repo, cfg, "text/plain")
            if item: refs.append(item)
        elif tools["yosys"]["available"]:
            tool = "yosys"
            command = [self.yosys, "-s", str(root / "equivalence.ys")]
            script = "\n".join((
                "read_verilog -formal -sv reference.sv",
                f"prep -top {top_module} -flatten",
                "rename -top gold",
                "design -stash gold",
                "read_verilog -formal -sv candidate.sv",
                f"prep -top {top_module} -flatten",
                "rename -top gate",
                "design -stash gate",
                "design -reset",
                "design -copy-from gold -as gold gold",
                "design -copy-from gate -as gate gate",
                "equiv_make gold gate equiv",
                "hierarchy -top equiv",
                "equiv_simple",
                "equiv_status -assert",
                "",
            ))
            cfg = _safe_write(root / "equivalence.ys", script)
            item = _artifact(repo, cfg, "text/plain")
            if item: refs.append(item)
        else:
            result = EquivalenceResult(FormalStatus.INCONCLUSIVE, top_module, top_module, message="neither EQY nor Yosys is installed", artifacts=tuple(refs))
            return {"schema_version": SCHEMA_VERSION, **result.as_dict(), "ok": False}
        try:
            proc = self.runner(command, cwd=str(root), capture_output=True, text=True, check=False)
            rc, output = int(getattr(proc, "returncode", 1)), (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
        except (OSError, subprocess.SubprocessError) as exc:
            rc, output = 127, str(exc)
        status = _status_from_output(output, rc)
        cex_refs = []
        for path in (_counterexample_files(root) if status == FormalStatus.FAILED else []):
            item = _artifact(repo, path, "application/octet-stream")
            if item: cex_refs.append(item)
        raw = _safe_write(root / "equivalence-output.log", output)
        item = _artifact(repo, raw, "text/plain")
        if item: refs.append(item)
        result = EquivalenceResult(status, top_module, top_module, tool=tool, tool_version=tools[tool]["version"], message="counterexample found" if status == FormalStatus.FAILED else None, counterexample_artifact=(cex_refs[0]["sha256"] if cex_refs else None), artifacts=tuple(refs + cex_refs))
        payload = {"schema_version": SCHEMA_VERSION, **result.as_dict(), "ok": status == FormalStatus.PROVEN, "returncode": rc, "command": command, "raw_output": output, "counterexamples": cex_refs}
        result_path = _safe_write(root / "equivalence-result.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
        item = _artifact(repo, result_path, "application/json")
        if item: payload["artifacts"].append(item)
        return payload


async def run_formal_job(manager: Any, executor: FormalExecutor, verilog_code: str,
                         top_module: str, spec: Any | None = None, **kwargs: Any) -> dict[str, Any]:
    """Submit one formal run as a cancellable JobManager job."""
    holder: dict[str, Any] = {}

    async def runner(job: Any) -> str:
        result = await executor.execute_async(verilog_code, top_module, spec, job=job, **kwargs)
        holder["result"] = result
        job.manifest = result
        job.set_outcome("success" if result.get("status") == "proven" else "inconclusive" if result.get("status") in {"bounded", "inconclusive"} else "failure")
        return json.dumps(result, sort_keys=True)

    job = manager.submit("formal", {"top_module": top_module}, runner=runner, design=top_module)
    await job.wait()
    return holder["result"]


__all__ = ["SCHEMA_VERSION", "FormalStatus", "FormalProperty", "FormalPropertyResult", "EquivalenceResult", "FormalExecutor", "detect_formal_tools", "formal_properties_from_design_spec", "property_templates", "templates_for_design", "render_property_checker", "run_formal_job"]
