"""Discovery and capability reporting for the optional formal toolchain.

The formal tools are deliberately optional at import time.  A developer may
have only Yosys, or only a solver, and a missing executable must be reported as
an explicit capability gap rather than silently turning a formal result into a
pass.  The source installer in ``scripts/install-local-formal.sh`` places its
tools in ``.tools/eda/bin``; discovery also honours ``FORMAL_TOOLS_BIN`` and
the process ``PATH`` so system and container installations work unchanged.

This module only probes executable versions.  It does not run a proof and it
does not infer support for a solver from a package name.  Readiness flags are
conservative combinations of tools needed by the corresponding workflow:

* property checking requires Yosys, SymbiYosys (``sby``), and one SMT solver;
* equivalence uses EQY when present and a validated native-Yosys fallback otherwise.

The result is plain JSON-compatible data through :meth:`as_dict`, which makes
it suitable for evidence metadata and health endpoints without importing the
verification pipeline.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


FORMAL_TOOL_EXECUTABLES: dict[str, str] = {
    "yosys": "yosys",
    "sby": "sby",
    "eqy": "eqy",
    "z3": "z3",
    "boolector": "boolector",
    "yices": "yices-smt2",
}

# The first two are the solvers exercised by the installer.  Yices is
# recognised when a user supplies it, but is intentionally not installed by
# the project script because its upstream build is platform-specific.
SOLVER_NAMES = ("z3", "boolector", "yices")


def _version_tuple(text: str | None) -> tuple[int, int, int]:
    """Extract the first semantic-looking version from native tool output."""

    match = re.search(r"(?<!\d)(\d+)[.](\d+)(?:[.]([0-9]+))?", text or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(match.group(index) or 0) for index in range(1, 4))


@dataclass(frozen=True)
class ToolCapability:
    """One executable's observed version and availability."""

    name: str
    executable: str
    available: bool
    path: str | None = None
    version: str | None = None
    version_tuple: tuple[int, int, int] = (0, 0, 0)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "executable": self.executable,
            "available": self.available,
            "path": self.path,
            "version": self.version,
            "version_tuple": list(self.version_tuple),
        }
        if self.error:
            value["error"] = self.error
        return value


@dataclass(frozen=True)
class FormalToolchainCapabilities:
    """Observed formal tools and conservative workflow readiness."""

    tools: Mapping[str, ToolCapability]

    @property
    def solvers(self) -> tuple[str, ...]:
        return tuple(name for name in SOLVER_NAMES if self.tools[name].available)

    @property
    def property_ready(self) -> bool:
        return (self.tools["yosys"].available and self.tools["sby"].available
                and bool(self.solvers))

    @property
    def equivalence_ready(self) -> bool:
        return self.tools["yosys"].available

    @property
    def complete(self) -> bool:
        """Whether every supported component is installed.

        This is informational only: Yices is optional and therefore does not
        affect ``property_ready`` or ``equivalence_ready``.
        """

        return all(tool.available for tool in self.tools.values())

    def missing(self, workflow: str = "property") -> list[str]:
        """Return missing components for ``property`` or ``equivalence``."""

        if workflow == "equivalence":
            required = ["yosys"]
        elif workflow == "property":
            required = ["yosys", "sby"]
            if not self.solvers:
                required.append("one of: z3, boolector, yices")
        else:
            raise ValueError("workflow must be 'property' or 'equivalence'")
        return [name for name in required
                if name.startswith("one of:") or not self.tools[name].available]

    def as_dict(self) -> dict[str, Any]:
        return {
            "tools": {name: tool.as_dict() for name, tool in self.tools.items()},
            "solvers": list(self.solvers),
            "property": {"ready": self.property_ready,
                         "missing": self.missing("property")},
            "equivalence": {"ready": self.equivalence_ready,
                            "missing": self.missing("equivalence")},
            "complete": self.complete,
        }


def _candidate_path(name: str, *, env: Mapping[str, str],
                    which: Callable[..., str | None]) -> str | None:
    executable = FORMAL_TOOL_EXECUTABLES[name]
    explicit = env.get(f"FORMAL_{name.upper()}_BIN")
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None

    search_dirs: list[str] = []
    for variable in ("FORMAL_TOOLS_BIN", "EDA_TOOLS_BIN"):
        value = env.get(variable)
        if value:
            search_dirs.append(value)

    # This is intentionally derived from the repository, not from the user's
    # home directory.  It keeps local source builds discoverable when start.sh
    # has not been used to prepend PATH yet.
    project_bin = Path(__file__).resolve().parents[1] / ".tools" / "eda" / "bin"
    if project_bin.is_dir():
        search_dirs.append(str(project_bin))
    if search_dirs:
        path = os.pathsep.join(search_dirs + [env.get("PATH", "")])
        found = which(executable, path=path)
        if found:
            return found
    return which(executable)


def detect_tool(name: str, *, env: Mapping[str, str] | None = None,
                runner: Callable[..., Any] | None = None,
                which: Callable[..., str | None] | None = None) -> ToolCapability:
    """Probe one known tool without raising when it is absent or broken."""

    if name not in FORMAL_TOOL_EXECUTABLES:
        raise KeyError(f"unknown formal tool: {name}")
    environment = os.environ if env is None else env
    locate = shutil.which if which is None else which
    executable = FORMAL_TOOL_EXECUTABLES[name]
    path = _candidate_path(name, env=environment, which=locate)
    if not path:
        return ToolCapability(name, executable, False, error="executable not found")
    run = runner or subprocess.run
    try:
        process = run([path, "--version"], capture_output=True, text=True,
                      check=False, timeout=5)
        output = ((getattr(process, "stdout", "") or "") +
                  (getattr(process, "stderr", "") or "")).strip()
        code = int(getattr(process, "returncode", 1))
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        return ToolCapability(name, executable, False, path=path,
                              error=f"version probe failed: {exc}")
    if code != 0 or not output:
        return ToolCapability(name, executable, False, path=path,
                              version=output or None,
                              error=f"version probe exited {code}")
    return ToolCapability(name, executable, True, path=path, version=output,
                          version_tuple=_version_tuple(output))


def detect_formal_toolchain(*, env: Mapping[str, str] | None = None,
                            runner: Callable[..., Any] | None = None,
                            which: Callable[..., str | None] | None = None
                            ) -> FormalToolchainCapabilities:
    """Probe all known tools and return a conservative capability report."""

    tools = {name: detect_tool(name, env=env, runner=runner, which=which)
             for name in FORMAL_TOOL_EXECUTABLES}
    return FormalToolchainCapabilities(tools)


# Friendly aliases for callers that used the shorter terminology while the
# formal phase was being designed.
detect_formal_capabilities = detect_formal_toolchain
FormalCapabilities = FormalToolchainCapabilities


__all__ = [
    "FORMAL_TOOL_EXECUTABLES", "SOLVER_NAMES", "ToolCapability",
    "FormalToolchainCapabilities", "FormalCapabilities", "detect_tool",
    "detect_formal_toolchain", "detect_formal_capabilities",
]
