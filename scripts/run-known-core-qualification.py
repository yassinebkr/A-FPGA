#!/usr/bin/env python3
"""Import and lint a pinned known-core source set without model calls.

The caller performs the network fetch/FuseSoC setup separately. This command
only consumes local immutable inputs, stores a content-addressed hardware
project, and emits a digest-bound JSON lint report.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from core_fixtures import (
    apply_execution_profile,
    fixture_for,
    fixture_summary,
    validate_import,
)
from hardware_project import HardwareProjectUpdate, SourceInput, build_manifest
from multifile_eda import lint_manifest


def _read_direct(fixture, root: Path) -> HardwareProjectUpdate:
    files = {}
    for relative in fixture.compile_order:
        files[relative] = (root / relative).read_text()
    update = validate_import(fixture, files)
    return update.model_copy(update={"provenance": {
        "name": fixture.display_name,
        "role": "control" if fixture.control_fixture else "qualification target",
        "upstream": fixture.source.url,
        "commit": fixture.source.commit,
        "license_spdx": fixture.license_spdx,
        "import": "pinned-direct",
    }})


def _read_fusesoc(fixture, root: Path, file_list: Path) -> HardwareProjectUpdate:
    sources = []
    include_dirs = []
    defines = {}
    seen = set()
    for raw in file_list.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("+incdir+"):
            path = Path(line.removeprefix("+incdir+"))
            relative = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
            include_dirs.append(relative)
            continue
        if line.startswith("-D") and len(line) > 2:
            name, separator, value = line[2:].partition("=")
            defines[name] = value if separator else None
            continue
        path = Path(line)
        if path.suffix.lower() not in {".v", ".sv"}:
            continue
        absolute = path if path.is_absolute() else root / path
        relative = absolute.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        role = "package" if relative.endswith(("_pkg.sv", "_pkg.v")) else "rtl"
        language = "systemverilog" if absolute.suffix.lower() == ".sv" else "verilog"
        sources.append(SourceInput(
            path=relative, content=absolute.read_text(), role=role, language=language
        ))
    # Include directories may contain macro headers with a .sv extension (for
    # example lowRISC prim_assert.sv). They must be content-addressed too, but
    # are not standalone compilation units.
    for relative_dir in include_dirs:
        directory = root / relative_dir
        if not directory.is_dir():
            raise SystemExit(f"FuseSoC include directory is missing: {relative_dir}")
        for header in sorted(directory.iterdir()):
            if not header.is_file() or header.suffix.lower() not in {".v", ".sv", ".vh", ".svh"}:
                continue
            relative = header.relative_to(root).as_posix()
            if relative in seen:
                continue
            seen.add(relative)
            sources.append(SourceInput(
                path=relative, content=header.read_text(), role="header",
                language="systemverilog-header" if header.suffix.lower() in {".sv", ".svh"} else "verilog-header",
            ))
    if not sources:
        raise SystemExit("FuseSoC file list contains no Verilog/SystemVerilog sources")
    return HardwareProjectUpdate(
        top_module=fixture.top_module,
        files=sources,
        compile_order=[source.path for source in sources if source.role in {"rtl", "package"}],
        include_dirs=list(dict.fromkeys(include_dirs)),
        defines=defines,
        build_targets={fixture.qualification_profile: {
            "fixture": fixture.key,
            "resolver": "fusesoc",
            "source_commit": fixture.source.commit,
            "parameters": dict(fixture.parameters),
        }},
        provenance={
            "name": fixture.display_name,
            "role": "control" if fixture.control_fixture else "qualification target",
            "upstream": fixture.source.url,
            "commit": fixture.source.commit,
            "license_spdx": fixture.license_spdx,
            "import": "pinned-fusesoc-resolved",
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, choices=("picorv32-control", "ibex-minimal"))
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--fusesoc-file-list", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--execution-profile",
        choices=("source-validation", "synthesis", "formal"),
        default="source-validation",
        help="separate source validation, physical synthesis, and RVFI formal configurations",
    )
    args = parser.parse_args()
    fixture = fixture_for(args.fixture)
    root = args.source_root.resolve()
    if args.fusesoc_file_list:
        update = _read_fusesoc(fixture, root, args.fusesoc_file_list.resolve())
    else:
        update = _read_direct(fixture, root)
    update = apply_execution_profile(fixture, update, args.execution_profile)
    manifest = build_manifest(update)
    lint = lint_manifest(manifest)
    report = {
        "schema": "known-core-qualification/v1",
        "fixture": fixture_summary(fixture),
        "execution_profile": args.execution_profile,
        "hardware_project": manifest,
        "lint": lint,
        "status": "lint_passed" if lint["ok"] else "lint_failed",
        "remaining_gates": [
            "independent ISA compliance",
            "RVFI formal",
            "Sky130 synthesis and STA",
            "one bounded place-and-route",
        ],
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(encoded)
        temporary.replace(args.output)
    else:
        print(encoded, end="")
    return 0 if lint["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
