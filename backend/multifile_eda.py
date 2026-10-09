"""Trusted execution boundary for content-addressed hardware projects.

This module accepts a validated hardware-project manifest, never arbitrary
host paths or shell commands. It materializes the exact source set beneath a
digest-named directory and builds argv lists for supported tools.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hardware_project
import lint as lint_mod
from eda_workspace import EdaWorkspace
from hardware_project import HardwareProjectError, validate_manifest


class MultiFileEdaError(RuntimeError):
    pass


@dataclass(frozen=True)
class MaterializedProject:
    root: Path
    digest: str
    top_module: str
    sources: tuple[Path, ...]
    testbenches: tuple[Path, ...]
    constraints: tuple[Path, ...]
    include_dirs: tuple[Path, ...]
    defines: tuple[tuple[str, str | None], ...]
    build_targets: dict[str, dict[str, Any]]


def _safe_child(root: Path, relative: str) -> Path:
    path = root.joinpath(*relative.split("/"))
    resolved_root = root.resolve()
    resolved_parent = path.parent.resolve()
    if resolved_root != resolved_parent and resolved_root not in resolved_parent.parents:
        raise MultiFileEdaError(f"materialized path escapes source root: {relative}")
    return path


def materialize_manifest(value: Any, *, destination: Path | None = None) -> MaterializedProject:
    manifest = validate_manifest(value)
    base = destination or (Path(EdaWorkspace().root) / "_hardware_projects")
    root = base / manifest["digest"]
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise MultiFileEdaError("materialization root must not be a symlink")

    entries = {entry["path"]: entry for entry in manifest["files"]}
    for relative, entry in entries.items():
        body = hardware_project.repository.get_artifact(entry["sha256"])
        if body is None:
            raise HardwareProjectError(f"missing artifact for {relative}")
        if hashlib.sha256(body).hexdigest() != entry["sha256"] or len(body) != entry["size"]:
            raise HardwareProjectError(f"artifact mismatch for {relative}")
        target = _safe_child(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise MultiFileEdaError(f"refusing symlink materialization target: {relative}")
        if not target.exists() or target.read_bytes() != body:
            temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
            temporary.write_bytes(body)
            os.replace(temporary, target)

    def paths(items):
        return tuple(_safe_child(root, item) for item in items)

    return MaterializedProject(
        root=root,
        digest=manifest["digest"],
        top_module=manifest["top_module"],
        sources=paths(manifest["compile_order"]),
        testbenches=paths(manifest.get("testbenches", [])),
        constraints=paths(manifest.get("constraints", [])),
        include_dirs=paths(manifest.get("include_dirs", [])),
        defines=tuple(sorted(manifest.get("defines", {}).items())),
        build_targets=manifest.get("build_targets", {}),
    )


def verilator_lint_argv(project: MaterializedProject) -> list[str]:
    argv = [
        "verilator", "--lint-only", "--language", "1800-2017",
        "-Wall", "-Wno-fatal", "--top-module", project.top_module,
    ]
    argv.extend(f"-I{path}" for path in project.include_dirs)
    argv.extend(f"-D{name}" if value is None else f"-D{name}={value}"
                for name, value in project.defines)
    argv.extend(str(path) for path in project.sources)
    return argv


def lint_manifest(value: Any, *, destination: Path | None = None,
                  timeout: float = 300) -> dict[str, Any]:
    project = materialize_manifest(value, destination=destination)
    if not shutil.which("verilator"):
        raise MultiFileEdaError("Verilator is required for manifest lint")
    argv = verilator_lint_argv(project)
    completed = subprocess.run(
        argv, cwd=project.root, capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    raw = (completed.stdout + completed.stderr).strip()
    diagnostics = lint_mod.parse_verilator_text(raw)
    result = {
        "schema": "hardware-project-lint/v1",
        "ok": completed.returncode == 0,
        "outcome": "success" if completed.returncode == 0 else "failure",
        "tool": "verilator",
        "tool_version": lint_mod.tool_version("verilator"),
        "hardware_project_digest": project.digest,
        "top_module": project.top_module,
        "ordered_sources": [
            str(path.relative_to(project.root)) for path in project.sources
        ],
        "command": argv,
        "returncode": completed.returncode,
        "diagnostics": diagnostics[:50],
        "diagnostics_total": len(diagnostics),
        "raw_output_tail": raw[-8000:],
    }
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    result["report_digest"] = hashlib.sha256(canonical).hexdigest()
    return result
