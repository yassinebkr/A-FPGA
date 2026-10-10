"""Unit coverage for C3's extracted gate-netlist boundaries."""

import hashlib
import json

import pytest

import gate_netlist
from gate_netlist_contract import GateNetlistError, canonical_bytes, sha256_json
from gate_netlist_query import bounded_graph
from gate_netlist_schema import artifact_summary, load_gate_netlist, unavailable


def _fixture_artifact():
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "counter8.yosys.json"
    return gate_netlist.build_gate_netlist(
        json.loads(fixture.read_text()), top_module="counter8",
        job_id="contract-test", source_sha256="a" * 64)


def test_contract_digest_and_schema_layers_are_independently_composable():
    artifact = _fixture_artifact()
    encoded = canonical_bytes(artifact)
    decoded = load_gate_netlist(json.loads(encoded))
    body = {key: value for key, value in decoded.items()
            if key != "artifact_sha256"}
    assert decoded["artifact_sha256"] == sha256_json(body)
    summary = artifact_summary(decoded, repository_sha256=hashlib.sha256(encoded).hexdigest())
    assert summary["available"] is True
    assert summary["top_module"] == "counter8"


def test_query_layer_rejects_unbounded_requests_without_touching_persistence():
    artifact = load_gate_netlist(_fixture_artifact())
    with pytest.raises(GateNetlistError, match="node limit"):
        bounded_graph(artifact, node_limit=2001)
    graph = bounded_graph(artifact, node_limit=4)
    assert graph["truncated"] is True
    assert graph["counts"]["nodes"] <= 4


def test_facade_exports_extracted_contracts_and_honest_unavailable_state():
    artifact = _fixture_artifact()
    assert gate_netlist.load_gate_netlist(artifact)["schema"] == "gate-netlist/v1"
    assert gate_netlist.bounded_graph(artifact)["schema"] == "gate-graph/v1"
    assert unavailable("missing evidence")["available"] is False

def test_schema_rejects_unicode_module_identifiers():
    artifact = _fixture_artifact()
    module = artifact["modules"].pop("counter8")
    artifact["modules"]["café"] = module
    artifact["identity"]["top_module"] = "café"
    body = {key: value for key, value in artifact.items()
            if key != "artifact_sha256"}
    artifact["artifact_sha256"] = sha256_json(body)
    with pytest.raises(GateNetlistError, match="invalid module name"):
        load_gate_netlist(artifact)

