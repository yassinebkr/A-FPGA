"""Safe filesystem and input boundaries for EDA jobs.

The agent supplies the top-level module and SiliconCompiler target as tool
arguments.  Keep those values separate from path construction: a Verilog
identifier is not automatically a safe filesystem path, and SiliconCompiler
target lookup must not become arbitrary attribute lookup.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path


# The agent-facing tools intentionally expose the demo targets we support in
# the UI/system prompt.  Keeping this list explicit also prevents values such
# as ``__dict__`` or ``../../...`` from reaching getattr(sc_targets, target).
SUPPORTED_TARGETS = frozenset({
    "freepdk45_demo",
    "asap7_demo",
    "skywater130_demo",
    "gf180_demo",
})

# We deliberately accept the portable, unescaped Verilog identifier subset.
# Escaped identifiers can contain arbitrary path separators and are not
# appropriate as build directory names.
_TOP_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TARGET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_top_module(top_module: str) -> str:
    """Validate and return a safe top-level Verilog module identifier."""
    if not isinstance(top_module, str) or not _TOP_MODULE_RE.fullmatch(top_module):
        raise ValueError(
            "top_module must be a simple Verilog identifier containing only "
            "letters, digits, and underscores (and not starting with a digit)"
        )
    return top_module


def validate_target(target: str) -> str:
    """Validate an agent-controlled SiliconCompiler target name.

    The allow-list is intentionally narrow.  Adding a supported target is a
    code/configuration change rather than allowing arbitrary module
    attributes to be selected by an LLM-produced string.
    """
    if not isinstance(target, str) or not _TARGET_RE.fullmatch(target):
        raise ValueError("target must be a simple SiliconCompiler target name")
    if target not in SUPPORTED_TARGETS:
        available = ", ".join(sorted(SUPPORTED_TARGETS))
        raise ValueError(f"unsupported SiliconCompiler target '{target}'; available: {available}")
    return target


class EdaWorkspace:
    """Resolve all generated EDA artifacts beneath one build root.

    ``Path.resolve`` is used before containment checks, so absolute paths,
    ``..`` components, and symlinked parents cannot escape ``SC_BUILDS``.
    Callers should obtain paths through this class instead of joining the
    model-controlled top module themselves.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None):
        raw_root = root if root is not None else os.environ.get("SC_BUILDS", "build")
        if not isinstance(raw_root, (str, os.PathLike)) or not str(raw_root).strip():
            raise ValueError("SC_BUILDS must name a non-empty directory")
        self.root = Path(raw_root).expanduser().resolve()

    def _contained(self, path: str | os.PathLike[str], *, allow_root: bool = True) -> Path:
        candidate = Path(path).expanduser().resolve()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                f"EDA build path '{candidate}' escapes SC_BUILDS '{self.root}'"
            ) from exc
        if not allow_root and not relative.parts:
            raise ValueError("EDA artifact path must be below SC_BUILDS")
        return candidate

    def build_dir(self, path: str | os.PathLike[str]) -> Path:
        """Validate an already-computed build directory."""
        return self._contained(path)

    def design_dir(self, top_module: str) -> Path:
        top = validate_top_module(top_module)
        return self._contained(self.root / top, allow_root=False)

    def fmax_dir(self, top_module: str) -> Path:
        top = validate_top_module(top_module)
        return self._contained(self.root / f"{top}_fmax", allow_root=False)

    def period_dir(self, top_module: str, period_ns: float) -> Path:
        top = validate_top_module(top_module)
        if not isinstance(period_ns, (int, float)) or not math.isfinite(period_ns) or period_ns <= 0:
            raise ValueError("clock period must be a finite positive number")
        return self._contained(self.fmax_dir(top) / f"{period_ns:.3f}ns", allow_root=False)

    def artifact(self, directory: str | os.PathLike[str], filename: str) -> Path:
        """Return a contained artifact path with no filename traversal."""
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            raise ValueError("EDA artifact filename must be a single path component")
        return self._contained(Path(directory) / filename, allow_root=False)

    def verilog_path(self, top_module: str) -> Path:
        top = validate_top_module(top_module)
        return self.artifact(self.design_dir(top), f"{top}.v")

    def testbench_path(self, top_module: str) -> Path:
        top = validate_top_module(top_module)
        return self.artifact(self.design_dir(top), f"{top}_tb.v")

    def write_verilog(self, verilog_code: str, top_module: str) -> Path:
        path = self.verilog_path(top_module)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(verilog_code)
        return path

    def write_testbench(self, testbench_code: str, top_module: str) -> Path:
        path = self.testbench_path(top_module)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(testbench_code)
        return path

