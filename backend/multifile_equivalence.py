"""Digest-bound native-SystemVerilog to converted-Verilog equivalence gate."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import hardware_project
from multifile_eda import materialize_manifest


SCHEMA = "multifile-conversion-equivalence/v1"
_IMAGE = re.compile(r"^[A-Za-z0-9][^\s@]*@sha256:[0-9a-f]{64}$")


class ConversionEquivalenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class EquivalenceConfig:
    container_image: str
    tool_identity: str
    timeout_s: float = 900

    def __post_init__(self) -> None:
        if not _IMAGE.fullmatch(self.container_image):
            raise ValueError("equivalence image must be digest pinned")
        if not self.tool_identity or self.timeout_s <= 0:
            raise ValueError("tool identity and positive timeout are required")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _canonical_digest(value: Mapping[str, Any]) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode())


def _run(argv: list[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          check=False, timeout=timeout, shell=False)


def run_conversion_equivalence(
    manifest_value: Mapping[str, Any],
    converted_verilog: str | Path,
    *,
    config: EquivalenceConfig,
    destination: str | Path,
    expected_converted_sha256: str | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Prove native elaboration equals its converted Verilog representation.

    Gold is independently elaborated by sv-elab/read_slang. Gate is read by
    Yosys's Verilog frontend. The same frontend is never used for both sides.
    """
    manifest = hardware_project.validate_manifest(dict(manifest_value), require_artifacts=True)
    converted = Path(converted_verilog).resolve()
    if not converted.is_file() or converted.is_symlink():
        raise ConversionEquivalenceError("converted artifact must be a regular file")
    candidate_sha = _sha(converted.read_bytes())
    if expected_converted_sha256 and candidate_sha != expected_converted_sha256:
        raise ConversionEquivalenceError("converted artifact digest mismatch")

    base = Path(destination).resolve()
    base.mkdir(parents=True, exist_ok=True)
    project = materialize_manifest(manifest, destination=base / "sources")
    work = base / f"{project.digest[:16]}-{candidate_sha[:16]}"
    work.mkdir(parents=True, exist_ok=True)
    script = work / "equivalence.ys"

    compile_paths = [f"/gold/{path}" for path in manifest["compile_order"]]
    options = [f"-I/gold/{path}" for path in manifest.get("include_dirs", [])]
    for name, value in sorted(manifest.get("defines", {}).items()):
        options.append(f"-D{name}" if value is None else f"-D{name}={value}")
    gold_read = "read_slang " + " ".join((*compile_paths, "--top", project.top_module, *options))
    script_text = "\n".join((
        gold_read,
        f"prep -top {project.top_module} -flatten",
        "async2sync",
        "memory_map",
        "opt_clean",
        "rename -top gold",
        "design -stash gold",
        "read_verilog -formal -sv /gate/candidate.v",
        f"prep -top {project.top_module} -flatten",
        "async2sync",
        "memory_map",
        "opt_clean",
        "rename -top gate",
        "design -stash gate",
        "design -reset",
        "design -copy-from gold -as gold gold",
        "design -copy-from gate -as gate gate",
        "equiv_make gold gate equiv",
        "hierarchy -top equiv",
        "equiv_simple",
        "equiv_induct -seq 12",
        "equiv_status -assert",
        "",
    ))
    script.write_text(script_text, encoding="utf-8")
    argv = [
        "docker", "run", "--rm", "--network=none", "--read-only",
        "--mount", f"type=bind,src={project.root},dst=/gold,readonly",
        "--mount", f"type=bind,src={converted.parent},dst=/gate,readonly",
        "--mount", f"type=bind,src={work},dst=/work",
        config.container_image, "yosys", "-Q", "-q", "-l", "/work/equivalence.log",
        "-m", "slang", "-s", "/work/equivalence.ys",
    ]
    # The gate mount has a stable name regardless of its source filename.
    if converted.name != "candidate.v":
        gate_link = work / "candidate.v"
        gate_link.write_bytes(converted.read_bytes())
        argv[argv.index(f"type=bind,src={converted.parent},dst=/gate,readonly")] = (
            f"type=bind,src={work},dst=/gate,readonly")
    execute = runner or _run
    try:
        proc = execute(argv, cwd=work, timeout=config.timeout_s)
        rc = int(proc.returncode)
        output = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    except subprocess.TimeoutExpired:
        rc, output = 124, "equivalence timed out"
    durable_log = work / "equivalence.log"
    if durable_log.is_file() and not durable_log.is_symlink():
        output = durable_log.read_text(encoding="utf-8", errors="replace") + output
    proven = rc == 0 and "Equivalence successfully proven" in output
    explicit_failure = "Found unproven $equiv cells" in output or "equiv_status -assert" in output and rc != 0
    status = "proven" if proven else "failed" if explicit_failure else "inconclusive"
    payload = {
        "schema": SCHEMA,
        "status": status,
        "ok": proven,
        "input_manifest_digest": project.digest,
        "converted_sha256": candidate_sha,
        "top_module": project.top_module,
        "gold_frontend": "sv-elab/read_slang",
        "gate_frontend": "yosys/read_verilog",
        "container_image": config.container_image,
        "tool_identity": config.tool_identity,
        "command": argv,
        "returncode": rc,
        "script_sha256": _sha(script_text.encode()),
        "output_tail": output[-16000:],
    }
    payload["report_digest"] = _canonical_digest(payload)
    (work / "equivalence-result.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


__all__ = ["ConversionEquivalenceError", "EquivalenceConfig", "run_conversion_equivalence"]
