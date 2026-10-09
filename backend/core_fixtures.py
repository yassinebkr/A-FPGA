"""Offline metadata and import checks for known RV32I qualification fixtures.

The fixture catalog deliberately contains provenance and qualification intent, not
upstream RTL. A caller must fetch the pinned source through its own controlled
import step and then pass the resulting paths to :func:`validate_import`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Mapping

from hardware_project import HardwareProjectUpdate, SourceInput, normalize_path


class CoreFixtureError(ValueError):
    """Raised when fixture metadata or an imported source set is unsafe/incomplete."""


_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_TOP = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_GITHUB = re.compile(r"^https://github\.com/[^/]+/[^/]+(?:\.git)?$")
_ALLOWED_LICENSES = {"Apache-2.0", "ISC", "MIT", "BSD-2-Clause", "BSD-3-Clause"}
_EXECUTION_PROFILES = {"source-validation", "synthesis", "formal"}


@dataclass(frozen=True)
class SourcePin:
    """Immutable upstream location used for a fixture import."""

    url: str
    commit: str | None
    ref: str


@dataclass(frozen=True)
class CoreFixture:
    """A source-free description of a known-core qualification fixture."""

    key: str
    display_name: str
    source: SourcePin
    license_spdx: str
    top_module: str
    source_roots: tuple[str, ...]
    compile_order: tuple[str, ...]
    include_dirs: tuple[str, ...] = ()
    defines: Mapping[str, str | None] = field(default_factory=dict)
    parameters: Mapping[str, str | int | bool | None] = field(default_factory=dict)
    expected_isa: str = "RV32I"
    memory_assumptions: str = ""
    tests: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    qualification_profile: str = "rv32i-known-core-v1"
    control_fixture: bool = False


# PicoRV32's pin is also used by SiliconCompiler's local demo metadata. Keeping
# that known pin here makes the control fixture reproducible without downloading
# or vendoring anything into this repository.
PICORV32 = CoreFixture(
    key="picorv32-control",
    display_name="PicoRV32 control fixture",
    source=SourcePin(
        url="https://github.com/YosysHQ/picorv32.git",
        commit="ef203c2b0a3fb793280f5114941416c425c5b461",
        ref="ef203c2b0a3fb793280f5114941416c425c5b461",
    ),
    license_spdx="ISC",
    top_module="picorv32",
    source_roots=("picorv32.v",),
    compile_order=("picorv32.v",),
    parameters={
        "ENABLE_TRACE": 0,
        "ENABLE_COUNTERS": 1,
        "ENABLE_REGS_DUALPORT": 1,
        "BARREL_SHIFTER": 0,
        "TWO_CYCLE_ALU": 0,
        "TWO_CYCLE_COMPARE": 0,
    },
    memory_assumptions="Native ready/valid memory interface; no cached memory or CSR platform is assumed.",
    tests=("riscv-formal RVFI checks", "independent RV32I directed smoke"),
    constraints=("explicit clock and reset assumptions required at import",),
    control_fixture=True,
)


# Ibex is pinned to the reviewed commit below. The source is intentionally not
# vendored; an importer must fetch this exact commit and record its resulting
# hardware-project manifest before qualification.
IBEX_MINIMAL = CoreFixture(
    key="ibex-minimal",
    display_name="Ibex minimal RV32I+Zca qualification fixture",
    source=SourcePin(
        url="https://github.com/lowRISC/ibex.git",
        commit="654ac71f91f7da5dc71942c9ba8d3103666c484e",
        ref="654ac71f91f7da5dc71942c9ba8d3103666c484e",
    ),
    license_spdx="Apache-2.0",
    top_module="ibex_core",
    source_roots=("rtl", "vendor/lowrisc_ip/prim"),
    compile_order=(
        "rtl/ibex_pkg.sv",
        "rtl/ibex_cheriot_pkg.sv",
        "rtl/ibex_cheriot_ex.sv",
        "rtl/ibex_alu.sv",
        "rtl/ibex_branch_predict.sv",
        "rtl/ibex_compressed_decoder.sv",
        "rtl/ibex_controller.sv",
        "rtl/ibex_cs_registers.sv",
        "rtl/ibex_csr.sv",
        "rtl/ibex_counter.sv",
        "rtl/ibex_decoder.sv",
        "rtl/ibex_ex_block.sv",
        "rtl/ibex_fetch_fifo.sv",
        "rtl/ibex_id_stage.sv",
        "rtl/ibex_if_stage.sv",
        "rtl/ibex_load_store_unit.sv",
        "rtl/ibex_multdiv_fast.sv",
        "rtl/ibex_multdiv_slow.sv",
        "rtl/ibex_prefetch_buffer.sv",
        "rtl/ibex_pmp.sv",
        "rtl/ibex_wb_stage.sv",
        "rtl/ibex_dummy_instr.sv",
        "rtl/ibex_core.sv",
    ),
    include_dirs=("rtl", "vendor/lowrisc_ip/prim"),
    defines={"SYNTHESIS": "1", "RVFI": "1"},
    parameters={
        "RV32E": 0,
        "RV32M": "none",
        "RV32B": "none",
        "RV32ZC": "zca",
        "DbgTriggerEn": 0,
        "SecureIbex": 0,
    },
    expected_isa="RV32IZca",
    memory_assumptions="Separate instruction/data handshake ports; boot address, memory map, and reset behavior are environment inputs.",
    tests=("Ibex Verilator smoke/regression", "RISC-V architectural tests", "RVFI/riscv-formal"),
    constraints=("reviewed clock/reset SDC required before physical qualification",),
)


_FIXTURES = {item.key: item for item in (PICORV32, IBEX_MINIMAL)}


def list_fixtures() -> list[dict[str, Any]]:
    """Return JSON-safe catalog summaries without source bodies."""
    return [fixture_summary(item) for item in _FIXTURES.values()]


def fixture_for(key: str) -> CoreFixture:
    """Return a fixture description, or fail closed for unknown keys."""
    try:
        return _FIXTURES[key]
    except KeyError as exc:
        raise CoreFixtureError(f"unknown core fixture: {key}") from exc


def fixture_summary(fixture: CoreFixture) -> dict[str, Any]:
    """Serialize metadata for logs/UI; never includes RTL contents."""
    validate_fixture(fixture, require_pinned=False)
    return {
        "key": fixture.key,
        "display_name": fixture.display_name,
        "source_url": fixture.source.url,
        "source_ref": fixture.source.ref,
        "commit": fixture.source.commit,
        "pinned": fixture.source.commit is not None,
        "license_spdx": fixture.license_spdx,
        "top_module": fixture.top_module,
        "source_roots": list(fixture.source_roots),
        "compile_order": list(fixture.compile_order),
        "include_dirs": list(fixture.include_dirs),
        "defines": dict(fixture.defines),
        "parameters": dict(fixture.parameters),
        "expected_isa": fixture.expected_isa,
        "memory_assumptions": fixture.memory_assumptions,
        "tests": list(fixture.tests),
        "constraints": list(fixture.constraints),
        "qualification_profile": fixture.qualification_profile,
        "control_fixture": fixture.control_fixture,
    }


def apply_execution_profile(
    fixture: CoreFixture,
    update: HardwareProjectUpdate,
    profile: str,
) -> HardwareProjectUpdate:
    """Derive an explicit, evidence-visible execution configuration.

    Verification instrumentation and manufacturable RTL are intentionally
    separate configurations. In particular, Ibex's ``RVFI`` define adds a
    verification-only interface containing unpacked arrays which is not part
    of the physical core interface. Formal keeps that instrumentation;
    synthesis removes only ``RVFI`` while retaining ``SYNTHESIS``.
    """
    validate_fixture(fixture)
    if profile not in _EXECUTION_PROFILES:
        raise CoreFixtureError(f"unknown execution profile: {profile}")
    defines = dict(update.defines)
    removed_defines: list[str] = []
    if fixture.key == IBEX_MINIMAL.key:
        if profile == "synthesis":
            if "RVFI" in defines:
                removed_defines.append("RVFI")
            defines.pop("RVFI", None)
            defines["SYNTHESIS"] = defines.get("SYNTHESIS") or "1"
        elif profile == "formal":
            defines["RVFI"] = defines.get("RVFI") or "1"
    if fixture.key == IBEX_MINIMAL.key and profile in {"synthesis", "formal"}:
        from ibex_specialization import specialize_ibex_update
        update = update.model_copy(update={"defines": defines})
        update, _ = specialize_ibex_update(update, fixture.parameters)
    targets = json.loads(json.dumps(update.build_targets))
    target = targets.setdefault(fixture.qualification_profile, {})
    target["execution_profile"] = profile
    target["removed_defines"] = removed_defines
    provenance = json.loads(json.dumps(update.provenance))
    provenance["execution_profile"] = profile
    return update.model_copy(update={
        "defines": defines,
        "build_targets": targets,
        "provenance": provenance,
    })


def validate_fixture(fixture: CoreFixture, *, require_pinned: bool = True) -> None:
    """Validate provenance/configuration metadata before any source import."""
    if not isinstance(fixture, CoreFixture):
        raise CoreFixtureError("fixture must be a CoreFixture")
    if not fixture.key or not fixture.display_name:
        raise CoreFixtureError("fixture key and display name are required")
    if not _GITHUB.fullmatch(fixture.source.url):
        raise CoreFixtureError("fixture source must be an HTTPS GitHub repository URL")
    if require_pinned and not _SHA1.fullmatch(fixture.source.commit or ""):
        raise CoreFixtureError(
            f"fixture {fixture.key} has no immutable commit pin; resolve {fixture.source.ref!r} first"
        )
    if fixture.source.commit is not None and not _SHA1.fullmatch(fixture.source.commit):
        raise CoreFixtureError("fixture commit must be a 40-character lowercase git SHA")
    if fixture.license_spdx not in _ALLOWED_LICENSES:
        raise CoreFixtureError(f"unsupported or missing SPDX license: {fixture.license_spdx!r}")
    if not _TOP.fullmatch(fixture.top_module):
        raise CoreFixtureError("fixture top_module must be a portable HDL identifier")
    for path in (*fixture.source_roots, *fixture.compile_order, *fixture.include_dirs):
        try:
            normalize_path(path, directory=path in fixture.include_dirs)
        except (TypeError, ValueError) as exc:
            raise CoreFixtureError(f"unsafe fixture path {path!r}: {exc}") from exc
    if len(set(fixture.compile_order)) != len(fixture.compile_order):
        raise CoreFixtureError("fixture compile_order contains duplicates")
    if not fixture.compile_order:
        raise CoreFixtureError("fixture compile_order cannot be empty")
    if not fixture.source_roots:
        raise CoreFixtureError("fixture must declare source roots")
    if not fixture.expected_isa or not fixture.tests or not fixture.memory_assumptions:
        raise CoreFixtureError("fixture must declare ISA, tests, and memory assumptions")


def validate_import(
    fixture: CoreFixture,
    files: Mapping[str, str],
    *,
    require_pinned: bool = True,
) -> HardwareProjectUpdate:
    """Validate fetched fixture files and convert them to a project update.

    ``files`` contains bodies fetched by the caller at the pinned commit. The
    helper intentionally does not fetch, compile, simulate, or infer hierarchy.
    Every file in the declared compile order must be present, and extra files are
    allowed only under declared source roots so vendor/support files are not
    silently omitted from the imported project.
    """
    validate_fixture(fixture, require_pinned=require_pinned)
    if not isinstance(files, Mapping) or not files:
        raise CoreFixtureError("imported fixture source set is empty")
    normalized: dict[str, str] = {}
    for raw_path, body in files.items():
        try:
            path = normalize_path(raw_path)
        except (TypeError, ValueError) as exc:
            raise CoreFixtureError(f"unsafe imported path {raw_path!r}: {exc}") from exc
        if not isinstance(body, str):
            raise CoreFixtureError(f"source body for {path} must be text")
        if path in normalized:
            raise CoreFixtureError(f"duplicate imported path: {path}")
        normalized[path] = body
    missing = [path for path in fixture.compile_order if path not in normalized]
    if missing:
        raise CoreFixtureError("import is missing compile-order files: " + ", ".join(missing))

    def under_root(path: str, root: str) -> bool:
        return path == root or path.startswith(root.rstrip("/") + "/")

    outside = [path for path in normalized
               if not any(under_root(path, root) for root in fixture.source_roots)]
    if outside:
        raise CoreFixtureError("import contains files outside declared source roots: " + ", ".join(outside))
    source_inputs = []
    for path, body in normalized.items():
        role = "package" if path.endswith(("_pkg.sv", "/prim_pkg.sv")) else "rtl"
        language = "systemverilog" if path.endswith((".sv", ".svh")) else "verilog"
        source_inputs.append(SourceInput(path=path, content=body, role=role, language=language))
    return HardwareProjectUpdate(
        top_module=fixture.top_module,
        files=source_inputs,
        compile_order=list(fixture.compile_order),
        include_dirs=list(fixture.include_dirs),
        defines=dict(fixture.defines),
        constraints=[],
        build_targets={fixture.qualification_profile: {
            "fixture": fixture.key,
            "source_url": fixture.source.url,
            "source_ref": fixture.source.ref,
            "source_commit": fixture.source.commit,
            "license_spdx": fixture.license_spdx,
            "parameters": dict(fixture.parameters),
        }},
        provenance={
            "name": fixture.display_name,
            "role": "control" if fixture.control_fixture else "qualification target",
            "upstream": fixture.source.url,
            "commit": fixture.source.commit,
            "license_spdx": fixture.license_spdx,
            "import": "pinned-direct",
        },
    )


def qualification_ready(key: str) -> bool:
    """Whether the catalog entry is safe to use as qualification evidence."""
    fixture = fixture_for(key)
    try:
        validate_fixture(fixture)
    except CoreFixtureError:
        return False
    return True


__all__ = [
    "CoreFixture", "CoreFixtureError", "IBEX_MINIMAL", "PICORV32", "SourcePin",
    "fixture_for", "fixture_summary", "list_fixtures", "qualification_ready",
    "validate_fixture", "validate_import",
]
