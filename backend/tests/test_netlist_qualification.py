"""C5 deterministic large-netlist qualification fixtures."""

import hashlib

import gate_netlist
from netlist_qualification import (
    benchmark_spec,
    build_benchmark_artifact,
    make_synthetic_yosys,
    qualify_benchmark,
)


def test_profiles_are_stable_and_large_profile_is_available_on_demand():
    assert benchmark_spec("small").cell_count == 8
    assert benchmark_spec("medium").module_count == 6
    assert benchmark_spec("large").cell_count >= 100_000
    first = make_synthetic_yosys("medium")
    second = make_synthetic_yosys("medium")
    assert gate_netlist.canonical_bytes(first) == gate_netlist.canonical_bytes(second)
    assert hashlib.sha256(gate_netlist.canonical_bytes(first)).hexdigest() == hashlib.sha256(
        gate_netlist.canonical_bytes(second)).hexdigest()


def test_medium_fixture_preserves_module_hierarchy_and_bounded_expansion():
    artifact = build_benchmark_artifact("medium")
    assert artifact["completeness"] == {
        "complete": True,
        "truncated": False,
        "truncated_reason": None,
        "unsupported_primitives": [],
        "black_box_modules": [],
        "unknown_direction_cells": 0,
        "module_count": 6,
        "cell_count": 1_505,
        "net_count": 1_512,
    }
    graph = gate_netlist.bounded_graph(artifact, depth=1, node_limit=500)
    assert graph["counts"]["nodes"] <= 500
    assert graph["truncated"] is False
    assert "logic_tile_0" in graph["modules"]
    expanded = gate_netlist.bounded_graph(artifact, depth=2, node_limit=500)
    assert expanded["truncated"] is True
    assert any("node limit" in reason for reason in expanded["truncated_reasons"])


def test_large_fixture_query_is_honestly_bounded_without_full_transfer():
    result = qualify_benchmark("large", large_cells=2_000, node_limit=128)
    assert result["artifact_cell_count"] == 2_000
    assert result["returned_nodes"] <= 128
    assert result["truncated"] is True
    assert result["bounded_graph_bytes"] < result["persisted_artifact_bytes"]
    assert result["complete"] is True


def test_qualification_round_trip_is_deterministic():
    first = qualify_benchmark("small", node_limit=64)
    second = qualify_benchmark("small", node_limit=64)
    stable_fields = (
        "benchmark", "requested_cells", "requested_modules", "raw_yosys_bytes",
        "persisted_artifact_bytes", "bounded_graph_bytes", "returned_nodes",
        "returned_cells", "truncated", "truncated_reasons", "artifact_cell_count",
        "artifact_net_count", "artifact_module_count", "complete",
    )
    assert {key: first[key] for key in stable_fields} == {
        key: second[key] for key in stable_fields}
