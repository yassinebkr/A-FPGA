"""Conservative Tiny Tapeout hardening adapter.

This module is intentionally a small boundary around the upstream Tiny
Tapeout support tool.  It prepares an isolated, content-addressed input
workspace and invokes ``tt_tool.py --harden`` only when the complete local
tool contract is present.  A missing template, support tool, Docker, PDK, or
configuration is *inconclusive*; it can never become a passing result.

The adapter does not claim foundry sign-off.  A successful result means only
that the selected Tiny Tapeout/LibreLane hardening command returned success
for the exact recorded inputs.
"""

from __future__ import annotations

import hashlib
import asyncio
import contextvars
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "tinytapeout-hardening/v1"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TT_MODULE = re.compile(r"\bmodule\s+(tt_um_[A-Za-z_][A-Za-z0-9_]*)\b")
_EVIDENCE_CONTEXT: contextvars.ContextVar[tuple[str, str, Any] | None] = contextvars.ContextVar("tapeout_evidence_context", default=None)

_SDC_FILES = {
    "PNR_SDC_FILE": "src/pnr.sdc",
    "SIGNOFF_SDC_FILE": "src/signoff.sdc",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _git_revision(path: Path) -> str | None:
    if not (path / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (completed.stdout or "").strip()
    return value if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None


def _inventory(workspace: Path) -> list[dict[str, Any]]:
    roots = [workspace / "src", workspace / "test", workspace / "info.yaml",
             workspace / "hardening-inputs.json", workspace / "hardening.log",
             workspace / "runs"]
    files: set[Path] = set()
    for root in roots:
        if root.is_file() and not root.is_symlink():
            files.add(root)
        elif root.is_dir():
            files.update(item for item in root.rglob("*") if item.is_file() and not item.is_symlink())
    items = []
    for item in sorted(files):
        relative = item.relative_to(workspace).as_posix()
        items.append({"path": relative, "sha256": _sha256(item.read_bytes()),
                      "size_bytes": item.stat().st_size})
    return items


def inspect_klayout_drc(workspace: Path | str) -> dict[str, Any]:
    """Classify the optional LibreLane KLayout DRC stage conservatively.

    LibreLane can finish successfully when ``RUN_KLAYOUT_DRC`` is disabled,
    and its final metrics then simply do not contain
    ``klayout__drc_error__count``.  A successful flow exit is therefore not
    evidence that this check ran.  This helper records that distinction and
    only returns ``passed`` when the run configuration enabled the stage and
    a numeric zero-error metric was emitted by the run.
    """
    root = Path(workspace).expanduser().resolve()
    config_path = next(
        (candidate for candidate in (root / "src" / "config_merged.json",
                                     root / "src" / "config.json")
         if candidate.is_file()),
        None,
    )
    config: dict[str, Any] = {}
    config_error: str | None = None
    if config_path is not None:
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config = loaded
            else:
                config_error = "configuration is not a JSON object"
        except (OSError, json.JSONDecodeError) as exc:
            config_error = f"could not read configuration: {exc}"

    enabled_value = config.get("RUN_KLAYOUT_DRC")
    enabled = enabled_value in (True, 1, "1", "true", "True", "TRUE")
    if enabled_value is None:
        return {
            "status": "inconclusive", "enabled": None, "metric": None,
            "reason": config_error or "RUN_KLAYOUT_DRC is not declared",
            "config": str(config_path) if config_path else None,
        }
    if not enabled:
        return {
            "status": "not_run", "enabled": False, "metric": None,
            "reason": config_error or "RUN_KLAYOUT_DRC is disabled",
            "config": str(config_path) if config_path else None,
        }

    metric: Any = None
    metric_key: str | None = None
    runs = root / "runs"
    state_files = sorted(runs.rglob("state_out.json")) if runs.is_dir() else []
    for state_file in state_files:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metrics = state.get("metrics") if isinstance(state, dict) else None
        if not isinstance(metrics, dict):
            continue
        for key, value in metrics.items():
            normalized = str(key).lower()
            if "klayout" in normalized and "drc" in normalized and "error" in normalized:
                metric_key, metric = str(key), value
        if metric_key is not None:
            break

    if metric_key is None:
        return {
            "status": "inconclusive", "enabled": True, "metric": None,
            "reason": "KLayout DRC enabled but klayout__drc_error__count was not reported",
            "config": str(config_path) if config_path else None,
        }
    try:
        count = int(metric)
    except (TypeError, ValueError):
        return {
            "status": "inconclusive", "enabled": True,
            "metric": {"key": metric_key, "value": metric},
            "reason": f"KLayout DRC metric {metric_key} is not an integer",
            "config": str(config_path) if config_path else None,
        }
    return {
        "status": "passed" if count == 0 else "failed", "enabled": True,
        "metric": {"key": metric_key, "value": count},
        "reason": None if count == 0 else f"KLayout DRC reported {count} errors",
        "config": str(config_path) if config_path else None,
    }



def _default_template() -> Path:
    return Path(__file__).resolve().parent.parent / "tapeout" / "ttsky-verilog-template"


@dataclass(frozen=True)
class TinyTapeoutConfig:
    """Local hardening configuration.

    ``template_dir`` and ``build_root`` are resolved and checked by the
    adapter.  ``tt_tool`` may be supplied explicitly for CI or an engineer's
    local checkout; otherwise the conventional ``tt/tt_tool.py`` location is
    discovered below the template.
    """

    template_dir: Path = field(default_factory=_default_template)
    build_root: Path | None = None
    shuttle: str = "ttsky"
    pdk_root: Path | None = None
    pdk: str | None = None

    tt_tool: Path | None = None
    timeout_s: float = 3600.0
    docker_bin: str = "docker"
    librelane_bin: str = "librelane"
    python_bin: str | None = None
    # The upstream template defaults this optional check off to save time.
    # Hardening evidence from this adapter requires it unless explicitly
    # disabled by an engineer who accepts an inconclusive result.
    run_klayout_drc: bool = True

    def resolved_build_root(self) -> Path:
        root = self.build_root
        if root is None:
            root = os.environ.get("TAPEOUT_BUILDS") or os.environ.get("SC_BUILDS") or "build/tapeout"
        return Path(root).expanduser().resolve()

    def resolved_python(self) -> str:
        return self.python_bin or os.environ.get("TT_PYTHON") or os.environ.get("LIBRELANE_PYTHON") or sys.executable

    def resolved_pdk_root(self) -> Path | None:
        raw = self.pdk_root
        if raw is None:
            raw = os.environ.get("PDK_ROOT")
        return Path(raw).expanduser().resolve() if raw else None


@dataclass(frozen=True)
class TinyTapeoutCapabilities:
    template_dir: str
    template_present: bool
    info_yaml_present: bool
    config_json_present: bool
    tt_tool: str | None
    support_tool_present: bool
    docker: str | None
    librelane: str | None
    pdk_root: str | None
    pdk: str | None
    ready: bool
    missing: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_dir": self.template_dir,
            "template_present": self.template_present,
            "info_yaml_present": self.info_yaml_present,
            "config_json_present": self.config_json_present,
            "tt_tool": self.tt_tool,
            "support_tool_present": self.support_tool_present,
            "docker": self.docker,
            "librelane": self.librelane,
            "pdk_root": self.pdk_root,
            "pdk": self.pdk,
            "ready": self.ready,
            "missing": list(self.missing),
        }


def _find_tt_tool(config: TinyTapeoutConfig, template: Path) -> Path | None:
    
    candidates: list[Path] = []
    if config.tt_tool is not None:
        candidates.append(Path(config.tt_tool).expanduser())
    env_tool = os.environ.get("TT_TOOL")
    if env_tool:
        candidates.append(Path(env_tool).expanduser())
    candidates.extend((template / "tt" / "tt_tool.py", template / "tt_tool.py"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None
def _find_librelane(config: TinyTapeoutConfig) -> str | None:
    executable = shutil.which(config.librelane_bin)
    if executable:
        return executable
    try:
        probe = subprocess.run([config.resolved_python(), "-c", "import librelane"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return f"{config.resolved_python()} -m librelane" if probe.returncode == 0 else None





def detect_tiny_tapeout_capabilities(
    config: TinyTapeoutConfig | None = None,
) -> TinyTapeoutCapabilities:
    """Report capability without running or downloading any external tool."""

    config = config or TinyTapeoutConfig()
    template = Path(config.template_dir).expanduser().resolve()
    template_present = template.is_dir()
    info_present = (template / "info.yaml").is_file()
    config_present = (template / "src" / "config.json").is_file()
    tt_tool = _find_tt_tool(config, template) if template_present else None
    docker = shutil.which(config.docker_bin)
    librelane = _find_librelane(config)
    pdk_root = config.resolved_pdk_root()
    pdk_name = config.pdk or os.environ.get("PDK")
    missing: list[str] = []
    if not template_present:
        missing.append("Tiny Tapeout template")
    if not info_present:
        missing.append("template info.yaml")
    if not config_present:
        missing.append("template src/config.json")
    if tt_tool is None:
        missing.append("tt/tt_tool.py")
    if docker is None:
        missing.append("Docker executable")
    if librelane is None:
        missing.append("LibreLane executable or Python module")
    if pdk_root is None or not pdk_root.is_dir():
        missing.append("PDK_ROOT")
    if not pdk_name:
        missing.append("PDK")
    return TinyTapeoutCapabilities(
        template_dir=str(template), template_present=template_present,
        info_yaml_present=info_present, config_json_present=config_present,
        tt_tool=str(tt_tool) if tt_tool else None,
        support_tool_present=tt_tool is not None, docker=docker, librelane=librelane,
        pdk_root=str(pdk_root) if pdk_root else None, pdk=pdk_name,
        ready=not missing, missing=tuple(missing),
    )


def _validate_top(top_module: str) -> str:
    if not isinstance(top_module, str) or not _IDENTIFIER.fullmatch(top_module):
        raise ValueError("top_module must be a portable Verilog identifier")
    return top_module


def _inside(path: Path, root: Path) -> Path:
    path = path.expanduser().resolve()
    try:
        relative = path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Tiny Tapeout path escapes build root: {path}") from exc
    if not relative.parts:
        raise ValueError("Tiny Tapeout workspace must be below its build root")
    return path


def _validate_shuttle(shuttle: str) -> str:
    if not isinstance(shuttle, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", shuttle):
        raise ValueError("shuttle must be a simple identifier")
    return shuttle


def _replace_info_yaml(text: str, *, top: str, title: str, author: str,
                       description: str, clock_hz: int, source: str) -> str:
    """Update the known template fields while preserving unrelated metadata."""
    values = {
        "title": title, "author": author, "description": description,
        "clock_hz": str(int(clock_hz)), "top_module": f"tt_um_{top}",
    }
    result = text
    for key, value in values.items():
        pattern = re.compile(rf"^(\s*{re.escape(key)}:\s*)(.*?)(\s*(?:#.*)?)$", re.MULTILINE)
        quoted = str(value) if key == "clock_hz" else json.dumps(str(value))
        result, count = pattern.subn(rf"\g<1>{quoted}\g<3>", result, count=1)
        if count == 0:
            raise ValueError(f"template info.yaml is missing project.{key}")
    source_pattern = re.compile(r"(?ms)(^\s*source_files:\s*\n)(.*?)(?=^\S|\Z)")
    result, count = source_pattern.subn(rf'\g<1>    - "{source}"\n', result, count=1)
    if count == 0:
        raise ValueError("template info.yaml is missing project.source_files")
    return result


def _prepare_workspace(wrapper_code: str, top_module: str, config: TinyTapeoutConfig,
                       *, title: str | None = None, author: str = "ASIC Agent",
                       description: str | None = None, clock_hz: int = 50_000_000) -> tuple[Path, dict[str, Any]]:
    top = _validate_top(top_module)
    if not isinstance(wrapper_code, str) or not wrapper_code.strip():
        raise ValueError("wrapper_verilog must not be empty")
    expected = f"tt_um_{top}"
    modules = set(_TT_MODULE.findall(wrapper_code))
    if expected not in modules:
        raise ValueError(f"wrapper_verilog must declare Tiny Tapeout top module {expected}")
    if clock_hz <= 0:
        raise ValueError("clock_hz must be positive")
    template = Path(config.template_dir).expanduser().resolve()
    if not template.is_dir():
        raise ValueError(f"Tiny Tapeout template does not exist: {template}")
    root = config.resolved_build_root()
    shuttle = _validate_shuttle(config.shuttle)
    workspace = _inside(root / f"{shuttle}-{top}", root)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, workspace, ignore=shutil.ignore_patterns("runs"))
    source_name = f"tt_um_{top}.v"
    source_path = workspace / "src" / source_name
    source_path.write_text(wrapper_code, encoding="utf-8")
    user_config_path = workspace / "src" / "user_config.json"
    try:
        user_config = json.loads(user_config_path.read_text(encoding="utf-8")) if user_config_path.is_file() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"template src/user_config.json is invalid JSON: {exc}") from exc
    if not isinstance(user_config, dict):
        raise ValueError("template src/user_config.json must be a JSON object")
    user_config["RUN_KLAYOUT_DRC"] = 1 if config.run_klayout_drc else 0
    user_config_path.write_text(json.dumps(user_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    info_path = workspace / "info.yaml"
    info_path.write_text(_replace_info_yaml(
        info_path.read_text(encoding="utf-8"), top=top,
        title=title or f"{top} Tiny Tapeout", author=author,
        description=description or f"Generated {top} design", clock_hz=clock_hz,
        source=source_name), encoding="utf-8")
    makefile = workspace / "test" / "Makefile"
    if makefile.is_file():
        make_text = makefile.read_text(encoding="utf-8")
        make_text, count = re.subn(r"(?m)^PROJECT_SOURCES\s*=.*$",
                                   f"PROJECT_SOURCES = {source_name}", make_text, count=1)
        if count == 0:
            raise ValueError("template test/Makefile is missing PROJECT_SOURCES")
        makefile.write_text(make_text, encoding="utf-8")
    tb = workspace / "test" / "tb.v"
    if tb.is_file():
        tb_text = tb.read_text(encoding="utf-8")
        tb_text, count = re.subn(r"\btt_um_[A-Za-z_][A-Za-z0-9_]*\s+user_project\b",
                                 f"{expected} user_project", tb_text, count=1)
        if count == 0:
            raise ValueError("template test/tb.v is missing user_project instantiation")
        tb.write_text(tb_text, encoding="utf-8")
    # LibreLane 3.0.14 warns (and silently falls back to generic constraints)
    # when either implementation or signoff SDC is absent.  Treat those files
    # as part of the hardening input contract so a staged workspace cannot
    # accidentally lose the reviewed Tiny Tapeout harness assumptions.
    config_documents: list[dict[str, Any]] = []
    for config_name in ("config.json", "user_config.json"):
        config_path = workspace / "src" / config_name
        if not config_path.is_file():
            continue
        try:
            config_documents.append(json.loads(config_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Tiny Tapeout {config_name} is not valid JSON: {exc}") from exc
    sdc_inputs: dict[str, dict[str, Any]] = {}
    for key, relative in _SDC_FILES.items():
        path = workspace / relative
        if not path.is_file():
            raise ValueError(f"Tiny Tapeout template is missing explicit {relative}")
        # LibreLane resolves dir:: relative to the config file directory
        # (src/), so src/pnr.sdc is referenced as dir::pnr.sdc.
        expected_ref = f"dir::{Path(relative).name}"
        if not any(document.get(key) == expected_ref for document in config_documents):
            raise ValueError(f"Tiny Tapeout config does not reference {relative} as {key}")
        sdc_inputs[key] = {
            "path": relative,
            "sha256": _sha256(path.read_bytes()),
            "size_bytes": path.stat().st_size,
        }
    inputs = {
        "schema_version": SCHEMA_VERSION, "shuttle": shuttle,
        "top_module": expected, "source_file": source_name,
        "wrapper_sha256": _sha256(wrapper_code.encode()),
        "info_yaml_sha256": _sha256(info_path.read_bytes()),
        "config_json_sha256": _sha256((workspace / "src" / "config.json").read_bytes()),
        "user_config_sha256": _sha256(user_config_path.read_bytes()),
        "sdc_files": sdc_inputs,
        "template_revision": _git_revision(template),
        "support_tools_revision": _git_revision(template / "tt"),
    }
    (workspace / "hardening-inputs.json").write_text(_canonical(inputs) + "\n", encoding="utf-8")
    return workspace, inputs



def _container_identity(config: Any, output: str) -> dict[str, str] | None:
    """Resolve the exact container digest used by a local hardening run."""
    image = os.environ.get("TAPEOUT_CONTAINER_IMAGE")
    declared_digest = os.environ.get("TAPEOUT_CONTAINER_DIGEST")
    if not image:
        match = re.search(r"(ghcr\.io/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?)", output)
        image = match.group(1) if match else None
    if image and declared_digest and re.fullmatch(r"sha256:[0-9a-f]{64}", declared_digest):
        return {"image": image, "digest": declared_digest, "runtime": "docker"}
    if not image:
        return None
    docker = getattr(config, "docker_bin", "docker")
    try:
        completed = subprocess.run(
            [docker, "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    reference = (completed.stdout or "").strip()
    digest = reference.rsplit("@", 1)[-1] if "@" in reference else ""
    if completed.returncode == 0 and re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return {"image": image, "digest": digest, "runtime": "docker"}
    return None

def _result(status: str, *, capabilities: TinyTapeoutCapabilities,
            workspace: Path | None = None, inputs: Mapping[str, Any] | None = None,
            reason: str | None = None, command: Sequence[str] = (), output: str = "",
            returncode: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "status": status,
        "ok": status == "passed", "manufacturing_candidate": False,
        "signoff": False, "assurance": "unclassified",
        "capabilities": capabilities.as_dict(), "reason": reason,
        "command": list(command), "returncode": returncode,
        "tool_output": output[-12000:] if output else "",
    }
    if workspace is not None:
        payload["artifacts"] = _inventory(workspace)
        payload["workspace"] = str(workspace)
        payload["checks"] = {"klayout_drc": inspect_klayout_drc(workspace)}
    if inputs is not None:
        payload["inputs"] = dict(inputs)
        payload["inputs_sha256"] = _sha256(_canonical(inputs).encode())
    context = _EVIDENCE_CONTEXT.get()
    if context is not None:
        wrapper_verilog, top_module, config = context
        container = _container_identity(config, output)
        if container is not None:
            payload["container"] = container
        try:
            from tapeout_repro import persist_tapeout_evidence
            payload["evidence"] = persist_tapeout_evidence(
                payload, wrapper_verilog=wrapper_verilog, top_module=top_module,
                workspace=workspace, config=config)
            payload["evidence_id"] = payload["evidence"]["id"]
            payload["evidence_digest"] = payload["evidence"]["digest"]
            payload["release_gate"] = payload["evidence"]["release_gate"]
        except Exception as exc:
            # Evidence failure is itself durable result metadata and never
            # changes a hardening result into a passing release claim.
            payload["evidence"] = {
                "status": "inconclusive",
                "error": f"could not persist typed evidence: {exc}",
            }
            payload["release_gate"] = {
                "eligible": False,
                "assurance": "local_dry_run",
                "reasons": ["typed evidence persistence failed"],
            }
        finally:
            # A result is the one consumer of the per-call context. Clearing
            # here prevents a later unrelated _result call from inheriting the
            # previous design's wrapper/config.
            _EVIDENCE_CONTEXT.set(None)
    return payload


def run_tiny_tapeout_hardening(
    wrapper_verilog: str,
    top_module: str,
    *,
    config: TinyTapeoutConfig | None = None,
    title: str | None = None,
    author: str = "ASIC Agent",
    description: str | None = None,



    clock_hz: int = 50_000_000,
) -> dict[str, Any]:
    """Prepare and, when possible, run the real Tiny Tapeout hardening flow.

    This is deliberately synchronous so callers can run it in a JobManager
    subprocess/thread boundary.  Missing prerequisites return ``inconclusive``
    and never invoke a best-effort substitute.
    """
    config = config or TinyTapeoutConfig()
    _EVIDENCE_CONTEXT.set((wrapper_verilog, top_module, config))
    capabilities = detect_tiny_tapeout_capabilities(config)
    try:
        workspace, inputs = _prepare_workspace(
            wrapper_verilog, top_module, config, title=title, author=author,
            description=description, clock_hz=clock_hz)
    except (OSError, ValueError) as exc:
        return _result("inconclusive", capabilities=capabilities,
                       reason=f"invalid or unavailable Tiny Tapeout input: {exc}")
    if not capabilities.ready:
        return _result("inconclusive", capabilities=capabilities,
                       workspace=workspace, inputs=inputs,
                       reason="required Tiny Tapeout hardening prerequisites are missing: " +
                       ", ".join(capabilities.missing))
    assert capabilities.tt_tool is not None
    # Run the staged support tool when it is part of the template checkout.
    tool_path = Path(capabilities.tt_tool)
    try:
        relative_tool = tool_path.relative_to(Path(config.template_dir).expanduser().resolve())
        staged_tool = workspace / relative_tool
        tool_path = staged_tool if staged_tool.is_file() else tool_path
    except ValueError:
        pass
    command = (config.resolved_python(), str(tool_path), "--harden")
    try:
        completed = subprocess.run(
            list(command), cwd=workspace, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=config.timeout_s, check=False,
            env={**os.environ, "PDK_ROOT": capabilities.pdk_root or "",
                 "PDK": capabilities.pdk or ""},
        )
        output = completed.stdout or ""
        (workspace / "hardening.log").write_text(output, encoding="utf-8")
        status = "passed" if completed.returncode == 0 else "failed"
        reason = None if status == "passed" else f"Tiny Tapeout hardening exited with code {completed.returncode}"
        return _result(status, capabilities=capabilities, workspace=workspace,
                       inputs=inputs, reason=reason, command=command,
                       output=output, returncode=completed.returncode)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return _result("failed", capabilities=capabilities, workspace=workspace,
                       inputs=inputs, reason=f"Tiny Tapeout hardening timed out after {config.timeout_s}s",
                       command=command, output=output, returncode=None)
    except OSError as exc:
        return _result("inconclusive", capabilities=capabilities, workspace=workspace,
                       inputs=inputs, reason=f"could not execute Tiny Tapeout tool: {exc}",
                       command=command)


async def run_tiny_tapeout_hardening_job(
    wrapper_verilog: str,
    top_module: str,
    *,
    config: TinyTapeoutConfig | None = None,
    session: str | None = None,
    manager: Any | None = None,
    title: str | None = None,
    author: str = "ASIC Agent",
    description: str | None = None,
    clock_hz: int = 50_000_000,
) -> str:
    """Run hardening as a streamed, cancellable JobManager process.

    Workspace staging is synchronous and bounded, but the support tool itself
    is always spawned by :meth:`jobs.Job.run_process`.  This keeps stdout in
    the normal job ring/disk log and lets JobManager terminate the complete
    process group on cancellation or timeout.
    """
    import jobs as jobs_module

    manager = manager or jobs_module.manager
    config = config or TinyTapeoutConfig()
    _EVIDENCE_CONTEXT.set((wrapper_verilog, top_module, config))

    async def runner(job: Any) -> str:
        capabilities = detect_tiny_tapeout_capabilities(config)
        try:
            workspace, inputs = _prepare_workspace(
                wrapper_verilog, top_module, config, title=title, author=author,
                description=description, clock_hz=clock_hz)
        except (OSError, ValueError) as exc:
            result = _result(
                "inconclusive", capabilities=capabilities,
                reason=f"invalid or unavailable Tiny Tapeout input: {exc}")
            job.set_outcome("inconclusive")
            job.manifest = {"tapeout": result}
            serialized = json.dumps(result, sort_keys=True)
            await job.emit(serialized)
            return serialized

        job.build_dir = str(workspace)
        if not capabilities.ready:
            result = _result(
                "inconclusive", capabilities=capabilities, workspace=workspace,
                inputs=inputs,
                reason="required Tiny Tapeout hardening prerequisites are missing: "
                + ", ".join(capabilities.missing))
            job.set_outcome("inconclusive")
            job.manifest = {"tapeout": result}
            serialized = json.dumps(result, sort_keys=True)
            await job.emit(serialized)
            return serialized

        assert capabilities.tt_tool is not None
        tool_path = Path(capabilities.tt_tool)
        try:
            relative_tool = tool_path.relative_to(
                Path(config.template_dir).expanduser().resolve())
            staged_tool = workspace / relative_tool
            tool_path = staged_tool if staged_tool.is_file() else tool_path
        except ValueError:
            pass
        command = [config.resolved_python(), str(tool_path), "--harden"]
        env = {"PDK_ROOT": capabilities.pdk_root or "", "PDK": capabilities.pdk or ""}
        try:
            returncode, output = await job.run_process(
                command, cwd=str(workspace), timeout=config.timeout_s, env=env)
            (workspace / "hardening.log").write_text(output, encoding="utf-8")
            status = "passed" if returncode == 0 else "failed"
            result = _result(
                status, capabilities=capabilities, workspace=workspace,
                inputs=inputs,
                reason=None if status == "passed" else
                f"Tiny Tapeout hardening exited with code {returncode}",
                command=command, output=output, returncode=returncode)
            job.set_outcome("success" if status == "passed" else "failure")
        except jobs_module.JobTimeoutError:
            result = _result(
                "failed", capabilities=capabilities, workspace=workspace,
                inputs=inputs,
                reason=f"Tiny Tapeout hardening timed out after {config.timeout_s}s",
                command=command, output="", returncode=None)
            job.set_outcome("failure")
        except asyncio.CancelledError:
            # JobManager normally kills an active child and lets the runner
            # unwind through EOF.  This branch covers cancellation during
            # process creation/staging and keeps the task cancellation-safe.
            job.set_outcome("failure")
            raise
        except OSError as exc:
            result = _result(
                "inconclusive", capabilities=capabilities, workspace=workspace,
                inputs=inputs,
                reason=f"could not execute Tiny Tapeout tool: {exc}",
                command=command)
            job.set_outcome("inconclusive")

        job.manifest = {"tapeout": result}
        serialized = json.dumps(result, sort_keys=True)
        await job.emit(serialized)
        return serialized

    job = manager.submit(
        "tapeout_hardening", {"top_module": top_module}, runner=runner,
        design=top_module, session=session)
    try:
        result = await job.wait()
    except jobs_module.JobCancelledError:
        return "Tiny Tapeout hardening CANCELLED by user."
    return f"{result}\njob_id: {job.id}"


__all__ = [
    "SCHEMA_VERSION", "TinyTapeoutCapabilities", "TinyTapeoutConfig",
    "detect_tiny_tapeout_capabilities", "run_tiny_tapeout_hardening",
    "run_tiny_tapeout_hardening_job", "inspect_klayout_drc",
]
