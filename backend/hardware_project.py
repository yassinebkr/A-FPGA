"""Versioned content-addressed multi-file hardware projects."""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from evidence import repository

SCHEMA = "hardware-project/v1"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_TOP = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_LANG = {".v": "verilog", ".sv": "systemverilog", ".vh": "verilog-header",
         ".svh": "systemverilog-header", ".sdc": "sdc"}


class HardwareProjectError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def normalize_path(value: str, *, directory: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("project path must be a non-empty string")
    value = value.replace("\\", "/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValueError("project paths must be relative")
    normalized = posixpath.normpath(value)
    if normalized in {".", ".."} or normalized.startswith("../"):
        raise ValueError("project path escapes the project root")
    if normalized != value.rstrip("/"):
        raise ValueError("project paths must be normalized")
    if not directory and value.endswith("/"):
        raise ValueError("file path cannot end with /")
    return normalized


class SourceInput(_Strict):
    path: str
    content: str
    role: Literal["rtl", "testbench", "constraint", "package", "header"] = "rtl"
    language: str | None = None

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return normalize_path(value)


class HardwareProjectUpdate(_Strict):
    top_module: str
    files: list[SourceInput] = Field(min_length=1, max_length=512)
    compile_order: list[str] | None = None
    include_dirs: list[str] = Field(default_factory=list, max_length=64)
    defines: dict[str, str | None] = Field(default_factory=dict)
    testbenches: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    build_targets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("top_module")
    @classmethod
    def valid_top(cls, value: str) -> str:
        if not _TOP.fullmatch(value):
            raise ValueError("top_module must be a portable HDL identifier")
        return value

    @model_validator(mode="after")
    def valid_references(self):
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("file paths must be unique")
        known = set(paths)
        order = self.compile_order
        if order is None:
            order = [item.path for item in self.files if item.role in {"rtl", "package"}]
        compilable = {item.path for item in self.files if item.role in {"rtl", "package"}}
        if len(order) != len(set(order)) or set(order) != compilable:
            raise ValueError("compile_order must contain every RTL/package file exactly once")
        for label, refs in (("testbenches", self.testbenches),
                            ("constraints", self.constraints)):
            if len(refs) != len(set(refs)) or not set(refs).issubset(known):
                raise ValueError(f"{label} must contain unique project file paths")
        for directory in self.include_dirs:
            normalize_path(directory, directory=True)
        if not any(re.search(rf"\bmodule\s+{re.escape(self.top_module)}\b", item.content)
                   for item in self.files if item.role in {"rtl", "package"}):
            raise ValueError("top_module is not declared by the supplied source set")
        return self


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def build_manifest(update: HardwareProjectUpdate, *, revision: int = 1) -> dict:
    files = []
    for source in update.files:
        body = source.content.encode()
        artifact = repository.put_artifact(body, name=source.path, media_type="text/plain")
        suffix = "." + source.path.rsplit(".", 1)[-1].lower() if "." in source.path else ""
        files.append({"path": source.path, "role": source.role,
                      "language": source.language or _LANG.get(suffix, "text"),
                      "sha256": artifact.sha256, "size": len(body)})
    core = {
        "schema": SCHEMA, "revision": revision, "top_module": update.top_module,
        "files": files,
        "compile_order": update.compile_order if update.compile_order is not None
                         else [item.path for item in update.files
                               if item.role in {"rtl", "package"}],
        "include_dirs": update.include_dirs, "defines": update.defines,
        "testbenches": update.testbenches, "constraints": update.constraints,
        "build_targets": update.build_targets,
        "provenance": update.provenance,
    }
    core["digest"] = _digest(core)
    return core


def validate_manifest(value: Any, *, require_artifacts: bool = True) -> dict:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise HardwareProjectError("unsupported hardware project schema")
    manifest = json.loads(json.dumps(value))
    expected = manifest.pop("digest", None)
    if not isinstance(expected, str) or not _SHA.fullmatch(expected) or _digest(manifest) != expected:
        raise HardwareProjectError("hardware project manifest digest mismatch")
    if not _TOP.fullmatch(str(manifest.get("top_module", ""))):
        raise HardwareProjectError("invalid top_module")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise HardwareProjectError("hardware project has no files")
    paths = []
    for entry in files:
        if not isinstance(entry, dict):
            raise HardwareProjectError("invalid file entry")
        try:
            path = normalize_path(entry.get("path"))
        except (TypeError, ValueError) as exc:
            raise HardwareProjectError(str(exc)) from exc
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not _SHA.fullmatch(digest):
            raise HardwareProjectError(f"invalid artifact digest for {path}")
        paths.append(path)
        if require_artifacts:
            body = repository.get_artifact(digest)
            if body is None:
                raise HardwareProjectError(f"missing artifact for {path}")
            if hashlib.sha256(body).hexdigest() != digest or len(body) != entry.get("size"):
                raise HardwareProjectError(f"artifact mismatch for {path}")
    if len(paths) != len(set(paths)):
        raise HardwareProjectError("duplicate file path")
    order = manifest.get("compile_order")
    compilable = {entry["path"] for entry in files if entry.get("role") in {"rtl", "package"}}
    if not isinstance(order, list) or len(order) != len(set(order)) or set(order) != compilable:
        raise HardwareProjectError("compile order does not match source files")
    manifest["digest"] = expected
    return manifest


def hydrate_manifest(value: Any) -> dict:
    result = validate_manifest(value)
    for entry in result["files"]:
        entry["content"] = repository.get_artifact(entry["sha256"]).decode()
    return result


def legacy_update(top_module: str, code: str) -> HardwareProjectUpdate:
    return HardwareProjectUpdate(top_module=top_module, files=[
        SourceInput(path=f"{top_module}.v", content=code, role="rtl")
    ])
