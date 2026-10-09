"""Digest-bound SystemVerilog-to-Verilog preprocessing.

The preprocessing boundary is deliberately separate from the EDA adapters.  A
validated :mod:`hardware_project` manifest is materialised read-only as input,
then an explicitly pinned sv2v container writes into a separate output mount.
The resulting artifact and report retain the source-set identity, tool
identity, exact argv, and hierarchy metadata.  This module does not perform or
claim functional/equivalence checking.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import hardware_project
from hardware_project import HardwareProjectError
from multifile_eda import materialize_manifest


SCHEMA = "hardware-project-sv2v-report/v1"
_IMAGE = re.compile(r"^[A-Za-z0-9][^\s@]*@sha256:[0-9a-f]{64}$")
_EXECUTABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_MAX_OUTPUT_BYTES = 256 * 1024 * 1024


class Sv2vPreprocessError(RuntimeError):
    """Raised when preprocessing cannot produce a trustworthy artifact."""


@dataclass(frozen=True)
class TrustedSv2vConfig:
    """Trusted, immutable execution configuration.

    ``container_image`` is intentionally required and must be digest pinned.
    It must come from application-owned configuration, not model/user input.
    ``tool_identity`` is recorded in evidence and should include the exact
    sv2v release or build identity used to create the image.
    """

    container_image: str
    tool_identity: str
    tool_executable: str = "sv2v"

    def __post_init__(self) -> None:
        if not _IMAGE.fullmatch(self.container_image):
            raise ValueError("sv2v container_image must be pinned with @sha256:<digest>")
        if (not self.tool_identity or len(self.tool_identity) > 256 or
                any(ord(char) < 32 for char in self.tool_identity)):
            raise ValueError("tool_identity must be a bounded, printable value")
        if not _EXECUTABLE.fullmatch(self.tool_executable):
            raise ValueError("tool_executable must be a portable executable name")


@dataclass(frozen=True)
class _RunnerResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[..., Any]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _tail(value: str, limit: int = 4000) -> str:
    return value[-limit:] if value else ""


def _run_argv(argv: list[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    """Run exactly argv; shell execution is deliberately impossible here."""
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout, check=False, shell=False)


def _normalise_runner_result(value: Any) -> _RunnerResult:
    if isinstance(value, _RunnerResult):
        return value
    if not hasattr(value, "returncode"):
        raise Sv2vPreprocessError("sv2v runner returned no process result")
    return _RunnerResult(
        returncode=int(value.returncode),
        stdout=str(getattr(value, "stdout", "") or ""),
        stderr=str(getattr(value, "stderr", "") or ""),
    )


def _validated_destination(destination: Path | None) -> Path:
    base = Path(destination or (Path.cwd() / "build" / "_sv2v_projects"))
    if base.exists() and base.is_symlink():
        raise Sv2vPreprocessError("sv2v destination must not be a symlink")
    base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink():
        raise Sv2vPreprocessError("sv2v destination must not be a symlink")
    return base.resolve()


def _source_hierarchy(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Copy source metadata without source bodies or mutable references."""
    return {
        "top_module": manifest["top_module"],
        "files": [
            {key: entry[key] for key in ("path", "role", "language", "sha256", "size")
             if key in entry}
            for entry in manifest["files"]
        ],
        "compile_order": list(manifest["compile_order"]),
        "include_dirs": list(manifest.get("include_dirs", [])),
        "defines": dict(manifest.get("defines", {})),
        "testbenches": list(manifest.get("testbenches", [])),
        "constraints": list(manifest.get("constraints", [])),
        "build_targets": json.loads(json.dumps(manifest.get("build_targets", {}))),
        "provenance": json.loads(json.dumps(manifest.get("provenance", {}))),
    }


def _docker_argv(project: Any, config: TrustedSv2vConfig) -> list[str]:
    source_mount = f"type=bind,src={project.root},dst=/input,readonly"
    argv = [
        "docker", "run", "--rm", "--network=none", "--read-only",
        "--mount", source_mount,
        config.container_image, config.tool_executable,
    ]
    argv.extend(f"-I/input/{path.relative_to(project.root).as_posix()}"
                for path in project.include_dirs)
    for name, value in project.defines:
        argv.append(f"-D{name}" if value is None else f"-D{name}={value}")
    argv.extend(f"/input/{path.relative_to(project.root).as_posix()}"
                for path in project.sources)
    return argv


def preprocess_manifest(
    value: Any,
    *,
    config: TrustedSv2vConfig,
    destination: Path | None = None,
    runner: Runner | None = None,
    timeout: float = 300,
) -> dict[str, Any]:
    """Preprocess a validated manifest and return an immutable evidence report.

    The supplied ``runner`` is only an injection seam for tests.  Production
    uses :func:`subprocess.run` through ``_run_argv`` and never invokes a shell.
    The runner receives ``(argv, cwd=output_dir, timeout=timeout)``.  Its
    process must write the requested ``/output/<file>`` path in the mounted
    output directory.
    """
    if not isinstance(config, TrustedSv2vConfig):
        raise Sv2vPreprocessError("trusted sv2v configuration is required")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise Sv2vPreprocessError("sv2v timeout must be positive")
    try:
        manifest = hardware_project.validate_manifest(value, require_artifacts=True)
    except (HardwareProjectError, TypeError, ValueError) as exc:
        raise Sv2vPreprocessError(f"invalid hardware project manifest: {exc}") from exc

    base = _validated_destination(destination)
    try:
        project = materialize_manifest(manifest, destination=base)
    except (HardwareProjectError, ValueError, OSError) as exc:
        raise Sv2vPreprocessError(f"cannot materialize hardware project: {exc}") from exc

    # The output is a sibling of the read-only source tree.  Never let a
    # converter write into the source materialization or the original store.
    output_dir = base / f".{project.digest}.sv2v-output"
    if output_dir.exists() and output_dir.is_symlink():
        raise Sv2vPreprocessError("sv2v output directory must not be a symlink")
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink():
        raise Sv2vPreprocessError("sv2v output directory must not be a symlink")
    output_name = f"{project.top_module}.v"
    output_path = output_dir / output_name
    if output_path.is_symlink():
        raise Sv2vPreprocessError("sv2v output must not be a symlink")
    if output_path.exists():
        output_path.unlink()

    argv = _docker_argv(project, config)
    execute = runner or _run_argv
    try:
        process = _normalise_runner_result(execute(argv, cwd=output_dir, timeout=timeout))
    except subprocess.TimeoutExpired as exc:
        raise Sv2vPreprocessError("sv2v preprocessing timed out") from exc
    except OSError as exc:
        raise Sv2vPreprocessError(f"sv2v runner failed to start: {exc}") from exc
    if process.returncode != 0:
        raise Sv2vPreprocessError(
            f"sv2v preprocessing failed with exit {process.returncode}: "
            f"{_tail(process.stderr or process.stdout)}"
        )
    # sv2v writes converted Verilog to stdout. Injectable test runners may
    # instead create output_path directly to exercise filesystem failures.
    if not output_path.exists() and process.stdout:
        output_path.write_text(process.stdout, encoding="utf-8")
    if not output_path.exists() or output_path.is_symlink() or not output_path.is_file():
        raise Sv2vPreprocessError("sv2v completed without a regular output artifact")
    output_size = output_path.stat().st_size
    if output_size > _MAX_OUTPUT_BYTES:
        raise Sv2vPreprocessError("sv2v output exceeds the configured size limit")
    output_bytes = output_path.read_bytes()
    raw_sha256 = hashlib.sha256(output_bytes).hexdigest()

    # Apply deterministic, reviewed post-sv2v adapter transforms.  These fix
    # known sv2v unpacked-to-packed array conversion issues without modifying
    # the upstream RTL.  Every transform is recorded in the evidence report.
    from sv2v_adapter import apply_adapter_idempotent
    adapter_result = apply_adapter_idempotent(output_bytes.decode("utf-8"))
    output_bytes = adapter_result.output_text.encode("utf-8")
    output_sha256 = hashlib.sha256(output_bytes).hexdigest()
    artifact = hardware_project.repository.put_artifact(
        output_bytes, name=f"{project.top_module}.sv2v.v", media_type="text/x-verilog",
        metadata={
            "kind": "sv2v_preprocessed_rtl",
            "input_manifest_digest": project.digest,
            "tool_identity": config.tool_identity,
            "adapter_transforms": len(adapter_result.transforms_applied),
        },
    )
    report = {
        "schema": SCHEMA,
        "status": "success",
        "input_manifest_digest": project.digest,
        "hardware_project_digest": project.digest,
        "tool_identity": config.tool_identity,
        "container_image": config.container_image,
        "argv": list(argv),
        "raw_output_sha256": raw_sha256,
        "output_sha256": output_sha256,
        "adapter": adapter_result.report(),
        "output_artifact": {
            "sha256": artifact.sha256,
            "name": artifact.name,
            "media_type": artifact.media_type,
            "size_bytes": artifact.size_bytes,
            "locator": artifact.locator,
        },
        "output_name": output_name,
        "source_hierarchy": _source_hierarchy(manifest),
        "runner": {
            "returncode": process.returncode,
            "stdout_tail": _tail(process.stdout),
            "stderr_tail": _tail(process.stderr),
        },
        "equivalence": {
            "status": "not_checked",
            "claim": False,
            "note": "sv2v preprocessing is a representation conversion; equivalence requires a separate verification gate.",
        },
    }
    report["report_digest"] = _digest(report)
    return report


# Descriptive aliases for callers that prefer an explicit operation name.
preprocess_hardware_project = preprocess_manifest
Sv2vConfig = TrustedSv2vConfig

