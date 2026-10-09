"""Conservative, digest-bound HDL declaration index for UI navigation.

This is deliberately not advertised as full SystemVerilog elaboration.  It
indexes exact module declarations and reports partial completeness; elaborated
instance hierarchy will later come from a compiler/netlist artifact.
"""
from __future__ import annotations

import re
from typing import Any

_MODULE = re.compile(r"(?m)^[ \t]*module\s+(?:automatic\s+)?([A-Za-z_][A-Za-z0-9_$]*)\b")


def build_module_index(project: dict[str, Any]) -> dict[str, Any]:
    modules = []
    duplicates = set()
    seen = set()
    for source in project.get("files", []):
        if source.get("role") not in {"rtl", "package"}:
            continue
        text = source.get("content")
        if not isinstance(text, str):
            continue
        # Remove comments without changing newline count, preserving locations.
        clean = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
        clean = re.sub(r"//[^\n]*", "", clean)
        for match in _MODULE.finditer(clean):
            name = match.group(1)
            if name in seen:
                duplicates.add(name)
            seen.add(name)
            modules.append({"name": name, "file": source.get("path"),
                            "line": clean.count("\n", 0, match.start()) + 1,
                            "sha256": source.get("sha256")})
    return {
        "schema": "module-index/v1",
        "hardware_project_digest": project.get("digest"),
        "top_module": project.get("top_module"),
        "completeness": "declarations_only",
        "modules": modules,
        "duplicate_modules": sorted(duplicates),
        "unresolved_instances": [],
        "limitations": ["Instance hierarchy requires elaboration evidence."],
    }
