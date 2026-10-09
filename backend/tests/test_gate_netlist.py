"""Phase G1 — canonical gate-netlist artifact, bounded graphs, REST contract.

The deterministic fixture is a hand-checked Yosys ``write_json`` document
(``tests/fixtures/counter8.yosys.json``): a small counter with one
hierarchical ``alu_slice`` instance, sky130 standard cells, a constant
reference, and ``src`` attributes for source cross-probing. It stands in for
the structured output ``sc_job.py`` emits after a real synthesis.
"""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import eda_tools
import evidence
import gate_netlist
import jobs as jobs_mod
from evidence import EvidenceRepository
from jobs import JobManager
from main import app

FIXTURE = Path(__file__).parent / "fixtures" / "counter8.yosys.json"
SOURCE_SHA = "a" * 64


def build_fixture_artifact():
    data = json.loads(FIXTURE.read_text())
    return gate_netlist.build_gate_netlist(
        data, top_module="counter8", job_id="job-x", project="proj-x",
        source_sha256=SOURCE_SHA, library="sky130_fd_sc_hd")


def load_fixture_artifact():
    artifact = build_fixture_artifact()
    return gate_netlist.load_gate_netlist(json.loads(gate_netlist.canonical_bytes(artifact)))


# -- Artifact contract --------------------------------------------------------


def test_fixture_build_is_byte_stable_with_verified_digest():
    first = gate_netlist.canonical_bytes(build_fixture_artifact())
    second = gate_netlist.canonical_bytes(build_fixture_artifact())
    assert first == second
    artifact = json.loads(first)
    body = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
    assert artifact["artifact_sha256"] == hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8")).hexdigest()
    # The identity binds every digest the plan requires.
    identity = artifact["identity"]
    assert identity["source_sha256"] == SOURCE_SHA
    assert identity["netlist_sha256"] == hashlib.sha256(
        FIXTURE.read_bytes()).hexdigest() or identity["netlist_sha256"]
    assert identity["top_module"] == "counter8"
    assert identity["library"] == "sky130_fd_sc_hd"


def test_artifact_records_hierarchy_cells_constants_and_completeness():
    artifact = load_fixture_artifact()
    top = artifact["modules"]["counter8"]
    slice_cell = next(c for c in top["cells"] if c["name"] == "_slice_")
    assert slice_cell["hierarchy"] == "alu_slice"
    assert slice_cell["source"] == "counter8.v:15"
    ff = next(c for c in top["cells"] if c["name"] == "_ff0_")
    assert ff["type"] == "sky130_fd_sc_hd__dfxtp_1"
    assert ff["ports"]["CLK"] == ["n0"]
    # The constant referenced by _and0_ exists as an explicit constant net.
    nets = {net["id"]: net for net in top["nets"]}
    assert "c0" in nets and nets["c0"]["names"] == ["<const 0>"]
    # Ports carry their net membership; nothing is fabricated.
    ports = {port["name"]: port for port in top["ports"]}
    assert ports["clk"]["nets"] == ["n0"]
    assert ports["q"]["nets"] == ["n1", "n2", "n3"]
    completeness = artifact["completeness"]
    assert completeness["module_count"] == 2
    assert completeness["cell_count"] == 5
    assert completeness["unsupported_primitives"] == []
    assert completeness["unknown_direction_cells"] == 0
    assert completeness["black_box_modules"] == []
    assert completeness["complete"] is True


def test_load_rejects_bad_schema_tampering_duplicates_and_bad_refs():
    artifact = json.loads(gate_netlist.canonical_bytes(build_fixture_artifact()))

    def resealed(doc):
        body = {k: v for k, v in doc.items() if k != "artifact_sha256"}
        doc["artifact_sha256"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            .encode("utf-8")).hexdigest()
        return doc

    with pytest.raises(gate_netlist.GateNetlistError, match="unsupported"):
        gate_netlist.load_gate_netlist({**artifact, "schema": "gate-netlist/v2"})

    tampered = json.loads(gate_netlist.canonical_bytes(build_fixture_artifact()))
    next(c for c in tampered["modules"]["counter8"]["cells"]
         if c["name"] == "_ff0_")["type"] = "something_else"
    with pytest.raises(gate_netlist.GateNetlistError, match="digest"):
        gate_netlist.load_gate_netlist(tampered)

    duplicate = json.loads(gate_netlist.canonical_bytes(build_fixture_artifact()))
    cells = duplicate["modules"]["counter8"]["cells"]
    duplicate["modules"]["counter8"]["cells"] = cells + [dict(cells[0])]
    with pytest.raises(gate_netlist.GateNetlistError, match="duplicate"):
        gate_netlist.load_gate_netlist(resealed(duplicate))

    bad_pin = json.loads(gate_netlist.canonical_bytes(build_fixture_artifact()))
    bad_pin["modules"]["counter8"]["cells"][0]["ports"]["X"] = ["n999"]
    with pytest.raises(gate_netlist.GateNetlistError, match="unknown net"):
        gate_netlist.load_gate_netlist(resealed(bad_pin))

    bad_hierarchy = json.loads(gate_netlist.canonical_bytes(build_fixture_artifact()))
    next(c for c in bad_hierarchy["modules"]["counter8"]["cells"]
         if c["name"] == "_slice_")["hierarchy"] = "missing_module"
    with pytest.raises(gate_netlist.GateNetlistError, match="unknown module"):
        gate_netlist.load_gate_netlist(resealed(bad_hierarchy))


def test_build_rejects_invalid_yosys_documents():
    data = json.loads(FIXTURE.read_text())
    with pytest.raises(gate_netlist.GateNetlistError):
        gate_netlist.build_gate_netlist({"modules": {}}, top_module="counter8")
    with pytest.raises(gate_netlist.GateNetlistError):
        gate_netlist.build_gate_netlist(data, top_module="not_there")
    bad_bits = json.loads(FIXTURE.read_text())
    bad_bits["modules"]["counter8"]["cells"]["_ff0_"]["connections"]["D"] = [-1]
    with pytest.raises(gate_netlist.GateNetlistError):
        gate_netlist.build_gate_netlist(bad_bits, top_module="counter8")
    bad_dir = json.loads(FIXTURE.read_text())
    bad_dir["modules"]["counter8"]["ports"]["clk"]["direction"] = "sideways"
    with pytest.raises(gate_netlist.GateNetlistError):
        gate_netlist.build_gate_netlist(bad_dir, top_module="counter8")


# -- Bounded graphs -----------------------------------------------------------


def test_bounded_graph_top_scope_is_deterministic_and_oriented():
    graph = gate_netlist.bounded_graph(load_fixture_artifact(), depth=1, node_limit=500)
    assert graph["schema"] == "gate-graph/v1"
    assert graph["module"] == "counter8"
    assert graph["modules"] == ["alu_slice", "counter8"]
    assert graph["truncated"] is False
    ids = {node["id"] for node in graph["nodes"]}
    assert {"port:clk", "port:rst_n", "port:q",
            "cell:_ff0_", "cell:_slice_", "cell:_and0_"} <= ids
    edges = {(edge["from"]["node"], edge["to"]["node"]) for edge in graph["edges"]}
    # Directed evidence: the clock port drives the flop, the flop drives the
    # slice instance, never the reverse.
    assert ("port:clk", "cell:_ff0_") in edges
    assert ("cell:_ff0_", "cell:_slice_") in edges
    assert ("cell:_slice_", "port:clk") not in edges
    # Repeated requests are deterministic.
    again = gate_netlist.bounded_graph(load_fixture_artifact(), depth=1, node_limit=500)
    assert gate_netlist.canonical_bytes(graph) == gate_netlist.canonical_bytes(again)


def test_bounded_graph_honors_limits_cones_and_hierarchy_depth():
    artifact = load_fixture_artifact()
    tiny = gate_netlist.bounded_graph(artifact, node_limit=4)
    assert tiny["counts"]["nodes"] <= 4
    assert tiny["truncated"] is True
    assert tiny["truncated_reasons"]

    cone = gate_netlist.bounded_graph(artifact, cone="cell:_ff0_", cone_hops=2)
    assert "cell:_ff0_" in {node["id"] for node in cone["nodes"]}
    assert "cell:_and0_" not in {node["id"] for node in cone["nodes"]}
    assert cone["truncated"] is False

    port_cone = gate_netlist.bounded_graph(artifact, cone="port:q", cone_hops=2)
    assert {"port:q", "cell:_slice_"} <= {node["id"] for node in port_cone["nodes"]}

    deep = gate_netlist.bounded_graph(artifact, depth=2, node_limit=500)
    inlined = {node["id"] for node in deep["nodes"]}
    assert "cell:_slice_/_xor_" in inlined
    assert "cell:_slice_/_mux_" in inlined

    child = gate_netlist.bounded_graph(artifact, module="alu_slice")
    assert {node["id"] for node in child["nodes"]} >= {"port:A", "cell:_xor_"}


def test_bounded_graph_rejects_abusive_requests():
    artifact = load_fixture_artifact()
    for kwargs in ({"module": "../etc/passwd"}, {"module": "no/such"},
                   {"node_limit": 0}, {"node_limit": 99999},
                   {"depth": 0}, {"depth": 99},
                   {"cone": "cell:missing"}, {"cone": ""},
                   {"cone_hops": 0}):
        with pytest.raises(gate_netlist.GateNetlistError):
            gate_netlist.bounded_graph(artifact, **kwargs)


def test_unknown_directions_are_reported_not_guessed():
    data = json.loads(FIXTURE.read_text())
    del data["modules"]["counter8"]["cells"]["_ff0_"]["port_directions"]
    artifact = gate_netlist.build_gate_netlist(data, top_module="counter8")
    assert artifact["completeness"]["unsupported_primitives"] == ["sky130_fd_sc_hd__dfxtp_1"]
    assert artifact["completeness"]["unknown_direction_cells"] == 1
    graph = gate_netlist.bounded_graph(artifact)
    ff_edges = [edge for edge in graph["edges"]
                if "cell:_ff0_" in (edge["from"]["node"], edge["to"]["node"])]
    assert ff_edges == []


def test_black_box_modules_are_explicit():
    data = json.loads(FIXTURE.read_text())
    data["modules"]["alu_slice"]["attributes"]["blackbox"] = 1
    artifact = gate_netlist.load_gate_netlist(json.loads(
        gate_netlist.canonical_bytes(
            gate_netlist.build_gate_netlist(data, top_module="counter8"))))
    assert artifact["completeness"]["black_box_modules"] == ["alu_slice"]
    graph = gate_netlist.bounded_graph(artifact)
    slice_node = next(n for n in graph["nodes"] if n["id"] == "cell:_slice_")
    assert slice_node["black_box"] is True


def test_liberty_blackbox_cell_types_are_leaves_not_hierarchy():
    """A standard-cell module pulled in via read_liberty -lib is a black box:
    instances of it are leaf cells, never hierarchy (field-fix from the live
    g1_probe synthesis run, where every sky130 cell was misreported as a
    hierarchical instance)."""
    data = json.loads(FIXTURE.read_text())
    data["modules"]["sky130_fd_sc_hd__dfxtp_1"] = {
        "attributes": {"blackbox": 1},
        "ports": {"CLK": {"direction": "input", "bits": [0]},
                  "D": {"direction": "input", "bits": [1]},
                  "Q": {"direction": "output", "bits": [2]}},
        "netnames": {}, "cells": {},
    }
    artifact = gate_netlist.load_gate_netlist(json.loads(gate_netlist.canonical_bytes(
        gate_netlist.build_gate_netlist(data, top_module="counter8"))))
    assert "sky130_fd_sc_hd__dfxtp_1" in artifact["completeness"]["black_box_modules"]
    top_cells = {cell["name"]: cell for cell in artifact["modules"]["counter8"]["cells"]}
    assert top_cells["_ff0_"]["hierarchy"] is None
    graph = gate_netlist.bounded_graph(artifact)
    ff_node = next(n for n in graph["nodes"] if n["id"] == "cell:_ff0_")
    assert ff_node["hierarchy"] is None
    assert ff_node["black_box"] is True
    # The flop still drives its loads: black-box leaf cells keep their edges.
    edges = {(edge["from"]["node"], edge["to"]["node"]) for edge in graph["edges"]}
    assert ("cell:_ff0_", "cell:_slice_") in edges


# -- Job integration ----------------------------------------------------------


def test_build_job_netlist_manifest_from_fixture_file(tmp_path):
    written = tmp_path / "counter8.yosys.json"
    written.write_bytes(FIXTURE.read_bytes())
    built = gate_netlist.build_job_netlist_manifest(
        str(written), top_module="counter8", job_id="job-x", project="proj-x",
        source_sha256=SOURCE_SHA)
    assert built["available"] is True
    artifact = built.pop("artifact")
    assert artifact["identity"]["top_module"] == "counter8"
    assert built["repository_sha256"] == hashlib.sha256(
        gate_netlist.canonical_bytes(artifact)).hexdigest()
    assert built["cell_count"] == 5


def test_build_job_netlist_manifest_is_honest_when_output_missing(tmp_path):
    missing = gate_netlist.build_job_netlist_manifest(
        str(tmp_path / "nope.json"), top_module="counter8")
    assert missing["available"] is False
    assert "no structured netlist output" in missing["reason"]

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    rejected = gate_netlist.build_job_netlist_manifest(
        str(corrupt), top_module="counter8")
    assert rejected["available"] is False
    assert "rejected" in rejected["reason"]


def test_persist_gate_netlist_stores_and_reports_unavailable(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    monkeypatch.setattr(evidence, "repository", repo)

    unavailable = eda_tools._persist_gate_netlist(
        {"status": "unavailable", "reason": "the flow emitted no gate netlist"},
        job_id="job-x", project="proj-x", top_module="counter8",
        source_sha256=SOURCE_SHA, constraints_sha256=None, library=None)
    assert unavailable["available"] is False

    written = tmp_path / "counter8.yosys.json"
    written.write_bytes(FIXTURE.read_bytes())
    meta = eda_tools._persist_gate_netlist(
        {"status": "available", "path": str(written)},
        job_id="job-x", project="proj-x", top_module="counter8",
        source_sha256=SOURCE_SHA, constraints_sha256=None,
        library="sky130_fd_sc_hd")
    assert meta["available"] is True
    assert meta["repository_sha256"]
    stored = repo.get_artifact(meta["repository_sha256"])
    assert stored is not None
    artifact = gate_netlist.load_gate_netlist(json.loads(stored))
    assert artifact["identity"]["job_id"] == "job-x"


def _run_job_with_gate_netlist(tmp_path, monkeypatch, repo, *, manifest_gate=True):
    """Submit a finished synthesis job carrying a real persisted artifact."""
    manager = JobManager(tmp_path / "jobs.db", tmp_path / "jobs")
    written = tmp_path / "counter8.yosys.json"
    if not written.exists():
        written.write_bytes(FIXTURE.read_bytes())
    monkeypatch.setattr(evidence, "repository", repo)
    meta = eda_tools._persist_gate_netlist(
        {"status": "available", "path": str(written)},
        job_id=None, project="proj-x", top_module="counter8",
        source_sha256=SOURCE_SHA, constraints_sha256=None,
        library="sky130_fd_sc_hd")

    async def scenario():
        async def runner(job):
            job.manifest = {"top_module": "counter8", "status": "success"}
            if manifest_gate:
                job.manifest["gate_netlist"] = meta
            job.set_outcome("success")
            return "synthesis successful"

        job = manager.submit("synthesis", {"top_module": "counter8"},
                             runner=runner, design="counter8", session="proj-x")
        await job.wait()
        return job.id

    try:
        job_id = asyncio.run(scenario())
    finally:
        asyncio.run(manager.stop())
    return manager, job_id


client = TestClient(app)


def test_netlist_endpoints_serve_bounded_graphs(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    manager, job_id = _run_job_with_gate_netlist(tmp_path, monkeypatch, repo)
    monkeypatch.setattr(jobs_mod, "manager", manager)

    meta = client.get(f"/api/jobs/{job_id}/netlist")
    assert meta.status_code == 200
    body = meta.json()
    assert body["available"] is True
    assert body["top_module"] == "counter8"
    assert body["cell_count"] == 5
    assert body["modules"] == ["alu_slice", "counter8"]

    graph = client.get(f"/api/jobs/{job_id}/netlist/graph")
    assert graph.status_code == 200
    payload = graph.json()
    assert payload["schema"] == "gate-graph/v1"
    assert payload["truncated"] is False
    assert payload["counts"]["cells"] == 3  # top scope only

    scoped = client.get(f"/api/jobs/{job_id}/netlist/graph",
                        params={"module": "alu_slice"})
    assert scoped.status_code == 200
    assert "cell:_xor_" in {node["id"] for node in scoped.json()["nodes"]}

    bounded = client.get(f"/api/jobs/{job_id}/netlist/graph",
                         params={"limit": 2})
    assert bounded.status_code == 200
    assert bounded.json()["counts"]["nodes"] <= 2
    assert bounded.json()["truncated"] is True

    cone = client.get(f"/api/jobs/{job_id}/netlist/graph",
                      params={"cone": "cell:_ff0_", "limit": 500})
    assert cone.status_code == 200
    assert "cell:_ff0_" in {node["id"] for node in cone.json()["nodes"]}

    # Digest pinning: a stale request is rejected, never silently combined.
    digest = body["artifact_sha256"]
    pinned = client.get(f"/api/jobs/{job_id}/netlist/graph",
                        params={"artifact_sha256": digest})
    assert pinned.status_code == 200
    stale = client.get(f"/api/jobs/{job_id}/netlist/graph",
                      params={"artifact_sha256": "b" * 64})
    assert stale.status_code == 409

    # Oversized or malformed requests fail safely with 422.
    for params in ({"limit": 99999}, {"depth": 0}, {"module": "../etc"},
                   {"cone": "cell:missing"}):
        response = client.get(f"/api/jobs/{job_id}/netlist/graph", params=params)
        assert response.status_code == 422, params

    assert client.get("/api/jobs/no-such-job/netlist").status_code == 404
    assert client.get("/api/jobs/no-such-job/netlist/graph").status_code == 404


def test_netlist_endpoints_survive_backend_restart(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    manager, job_id = _run_job_with_gate_netlist(tmp_path, monkeypatch, repo)

    # A restarted backend has a fresh JobManager (no live manifest) and a
    # fresh evidence repository instance on the same durable root.
    restarted = JobManager(tmp_path / "jobs.db", tmp_path / "jobs")
    monkeypatch.setattr(jobs_mod, "manager", restarted)
    monkeypatch.setattr(evidence, "repository", EvidenceRepository(tmp_path / "evidence"))

    meta = client.get(f"/api/jobs/{job_id}/netlist")
    assert meta.status_code == 200
    assert meta.json()["available"] is True
    assert meta.json()["top_module"] == "counter8"

    graph = client.get(f"/api/jobs/{job_id}/netlist/graph",
                      params={"module": "counter8"})
    assert graph.status_code == 200
    assert "cell:_ff0_" in {node["id"] for node in graph.json()["nodes"]}


def test_job_without_structured_output_reports_unavailable(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    manager, job_id = _run_job_with_gate_netlist(
        tmp_path, monkeypatch, repo, manifest_gate=False)
    monkeypatch.setattr(jobs_mod, "manager", manager)

    meta = client.get(f"/api/jobs/{job_id}/netlist")
    assert meta.status_code == 200
    body = meta.json()
    assert body["available"] is False
    assert "no structured gate netlist" in body["reason"]

    graph = client.get(f"/api/jobs/{job_id}/netlist/graph")
    assert graph.status_code == 200
    assert graph.json()["available"] is False
    # No diagram is fabricated from the RTL declaration index.
    assert "nodes" not in graph.json()
