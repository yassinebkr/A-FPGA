"""Deterministic backend qualification fixtures for large gate-netlist views.

The production gate viewer consumes Yosys ``write_json`` output.  These
fixtures exercise that same parser, artifact contract, and bounded graph query
without invoking an EDA tool or an LLM.  They are generated on demand so a
qualification run can select a genuinely large design without checking a
large binary document into the repository.

The generated designs are intentionally simple chains of directional AND
cells.  Their purpose is to measure data-shape and bound behavior, while the
small fixture also includes real module hierarchy for navigation tests.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

import gate_netlist
from gate_netlist_contract import sha256_json


@dataclass(frozen=True)
class BenchmarkSpec:
    """A reproducible synthetic netlist profile."""

    name: str
    cell_count: int
    module_count: int
    hierarchical: bool


BENCHMARKS = {
    "small": BenchmarkSpec("small", 8, 2, True),
    "medium": BenchmarkSpec("medium", 1_505, 6, True),
    "large": BenchmarkSpec("large", 100_000, 1, False),
}


def benchmark_spec(name: str, *, large_cells: int | None = None) -> BenchmarkSpec:
    """Return a named profile, optionally changing only the large size."""
    try:
        spec = BENCHMARKS[name]
    except KeyError as exc:
        raise ValueError(f"unknown benchmark {name!r}") from exc
    if name == "large" and large_cells is not None:
        if not isinstance(large_cells, int) or large_cells < 1:
            raise ValueError("large_cells must be a positive integer")
        return BenchmarkSpec(name, large_cells, 1, False)
    return spec


def _chain_module(name: str, cell_count: int, *, source_prefix: str) -> dict[str, Any]:
    """Build one valid Yosys module containing a directional cell chain."""
    if cell_count < 1:
        raise ValueError("chain modules require at least one cell")
    netnames = {
        f"net_{index}": {
            "bits": [index],
            "attributes": {"src": f"{source_prefix}.v:{index + 1}"},
        }
        for index in range(cell_count + 1)
    }
    cells = {
        f"u_{index:06d}": {
            "type": "$_AND_",
            "connections": {"A": [index], "B": ["0"], "Y": [index + 1]},
            "port_directions": {"A": "input", "B": "input", "Y": "output"},
            "attributes": {"src": f"{source_prefix}.v:{index + 1}"},
        }
        for index in range(cell_count)
    }
    return {
        "ports": {
            "a": {"direction": "input", "bits": [0]},
            "y": {"direction": "output", "bits": [cell_count]},
        },
        "netnames": netnames,
        "cells": cells,
    }


def _hierarchical_yosys(spec: BenchmarkSpec) -> dict[str, Any]:
    """Build small/medium profiles with reusable module boundaries."""
    child_count = 1 if spec.name == "small" else 5
    cells_per_child = 8 if spec.name == "small" else 300
    modules: dict[str, Any] = {
        f"logic_tile_{index}": _chain_module(
            f"logic_tile_{index}", cells_per_child,
            source_prefix=f"logic_tile_{index}")
        for index in range(child_count)
    }
    top_cells = {
        f"tile_{index}": {
            "type": f"logic_tile_{index}",
            "connections": {"a": [0], "y": [1]},
            "port_directions": {"a": "input", "y": "output"},
            "attributes": {"src": f"top.v:{index + 1}"},
        }
        for index in range(child_count)
    }
    modules["top"] = {
        "ports": {
            "a": {"direction": "input", "bits": [0]},
            "y": {"direction": "output", "bits": [1]},
        },
        "netnames": {
            "top_a": {"bits": [0], "attributes": {"src": "top.v:1"}},
            "top_y": {"bits": [1], "attributes": {"src": "top.v:2"}},
        },
        "cells": top_cells,
    }
    return {"creator": "netlist_qualification", "modules": modules}


def make_synthetic_yosys(name: str = "small", *, large_cells: int | None = None) -> dict[str, Any]:
    """Return deterministic Yosys JSON for a qualification profile."""
    spec = benchmark_spec(name, large_cells=large_cells)
    if spec.hierarchical:
        return _hierarchical_yosys(spec)
    return {
        "creator": "netlist_qualification",
        "modules": {"top": _chain_module("top", spec.cell_count, source_prefix="top")},
    }


def build_benchmark_artifact(name: str = "small", *, large_cells: int | None = None) -> dict[str, Any]:
    """Build and validate the same canonical artifact used by production."""
    yosys = make_synthetic_yosys(name, large_cells=large_cells)
    return gate_netlist.load_gate_netlist(
        gate_netlist.build_gate_netlist(
            yosys,
            top_module="top",
            job_id=f"qualification-{name}",
            project="qualification-fixtures",
            source_sha256="1" * 64,
            constraints_sha256="2" * 64,
            netlist_sha256=sha256_json(yosys),
            tool="synthetic-yosys",
            tool_version="qualification-v1",
            library="synthetic",
        )
    )


def qualify_benchmark(
    name: str,
    *,
    large_cells: int | None = None,
    node_limit: int = 500,
) -> dict[str, Any]:
    """Measure deterministic build, persistence round-trip, and graph latency."""
    spec = benchmark_spec(name, large_cells=large_cells)
    started = time.perf_counter()
    raw = make_synthetic_yosys(name, large_cells=large_cells)
    generated_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    artifact = build_benchmark_artifact(name, large_cells=large_cells)
    build_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    encoded = gate_netlist.canonical_bytes(artifact)
    persisted = json.loads(encoded)
    loaded = gate_netlist.load_gate_netlist(persisted)
    round_trip_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    graph = gate_netlist.bounded_graph(loaded, depth=2, node_limit=node_limit)
    graph_ms = (time.perf_counter() - started) * 1000
    graph_bytes = len(gate_netlist.canonical_bytes(graph))
    raw_bytes = len(gate_netlist.canonical_bytes(raw))
    completeness = artifact["completeness"]
    return {
        "benchmark": spec.name,
        "requested_cells": spec.cell_count,
        "requested_modules": spec.module_count,
        "generated_ms": round(generated_ms, 3),
        "artifact_build_ms": round(build_ms, 3),
        "persistence_round_trip_ms": round(round_trip_ms, 3),
        "bounded_graph_ms": round(graph_ms, 3),
        "raw_yosys_bytes": raw_bytes,
        "persisted_artifact_bytes": len(encoded),
        "bounded_graph_bytes": graph_bytes,
        "bounded_node_limit": node_limit,
        "returned_nodes": graph["counts"]["nodes"],
        "returned_cells": graph["counts"]["cells"],
        "truncated": graph["truncated"],
        "truncated_reasons": graph["truncated_reasons"],
        "artifact_cell_count": completeness["cell_count"],
        "artifact_net_count": completeness["net_count"],
        "artifact_module_count": completeness["module_count"],
        "complete": completeness["complete"],
        "limitations": [
            "synthetic parser/graph qualification; no EDA invocation",
            "browser heap, worker layout, and interaction latency require the frontend harness",
        ],
    }


def qualify_all(*, large_cells: int | None = None, node_limit: int = 500) -> list[dict[str, Any]]:
    return [qualify_benchmark(name, large_cells=large_cells, node_limit=node_limit)
            for name in ("small", "medium", "large")]


def _main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", choices=tuple(BENCHMARKS), action="append")
    parser.add_argument("--large-cells", type=int, default=None,
                        help="override the generated large profile size")
    parser.add_argument("--node-limit", type=int, default=500)
    args = parser.parse_args(argv)
    names = args.benchmark or ["small", "medium", "large"]
    report = [qualify_benchmark(name, large_cells=args.large_cells,
                                node_limit=args.node_limit) for name in names]
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a CLI
    raise SystemExit(_main())
