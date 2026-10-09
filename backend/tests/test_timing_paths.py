"""Phase G3 — ordered timing-path artifact, netlist mapping, REST contract.

The fixture report (``tests/fixtures/timing.setup.topN.rpt``) is an exact
OpenSTA ordered-path table (two setup paths through the counter8 gate
netlist fixture), so ordered cell/net mapping and delay totals are proven
against the same netlist the graph endpoint serves.
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
import timing_paths
from evidence import EvidenceRepository
from jobs import JobManager
from main import app

FIXTURE = Path(__file__).parent / "fixtures"
NETLIST_FIXTURE = FIXTURE / "counter8.yosys.json"
REPORT_FIXTURE = FIXTURE / "timing.setup.topN.rpt"

HOLD_REPORT = """Startpoint: _and0_ (input port clocked by clk)
Endpoint: _ff0_ (rising edge-triggered flip-flop clocked by clk)
Path Group: clk
Path Type: min
Corner: fast

Fanout     Cap    Slew   Delay    Time   Description
-----------------------------------------------------------------------------
                  0.00    0.00    0.00   clock clk (rise edge)
                          0.00    0.00   clock network delay (ideal)
                  0.00    0.00    0.00 ^ _and0_/A (sky130_fd_sc_hd__and2_0)
     2    0.02    0.91    0.31    0.31 ^ _and0_/X (sky130_fd_sc_hd__and2_0)
                                         n3 (net)
                  0.91    0.12    0.43 ^ _ff0_/D (sky130_fd_sc_hd__dfxtp_1)
                                  0.43   data arrival time

                  0.00    0.20    0.20   clock clk (rise edge)
                          0.00    0.20   clock network delay (ideal)
                          0.10    0.30   clock uncertainty
                                  0.30   data required time
-----------------------------------------------------------------------------
                                  0.30   data required time
                                 -0.43   data arrival time
-----------------------------------------------------------------------------
                                 -0.13   slack (VIOLATED)
"""


def fixture_netlist_artifact():
    data = json.loads(NETLIST_FIXTURE.read_text())
    return gate_netlist.load_gate_netlist(json.loads(
        gate_netlist.canonical_bytes(
            gate_netlist.build_gate_netlist(data, top_module="counter8",
                                            job_id="job-x",
                                            netlist_sha256="e" * 64))))


# -- Ordered report parsing ---------------------------------------------------


def test_parse_ordered_report_keeps_segments_delays_and_totals():
    paths = timing_paths.parse_ordered_report(
        REPORT_FIXTURE.read_text(), filename_hint="setup.topN")
    assert len(paths) == 2
    first = paths[0]
    assert first.analysis == "setup"
    assert first.startpoint == "_ff0_"
    assert first.endpoint == "q[0]"
    assert first.clock == "clk"
    assert first.corner == "slow"
    assert first.path_group == "clk"
    assert first.arrival_ns == 1.75
    assert first.required_ns == 7.75
    assert first.slack_ns == 6.0
    assert first.met is True
    kinds = [(segment.kind, segment.name) for segment in first.segments]
    assert ("cell_pin", "_ff0_/CLK") in kinds
    assert ("cell_pin", "_ff0_/Q") in kinds
    assert ("net", "q[0]") in kinds
    assert ("port", "q[0]") in kinds
    q_pin = next(segment for segment in first.segments
                 if segment.name == "_ff0_/Q")
    assert q_pin.delay_ns == 1.75 and q_pin.time_ns == 1.75
    assert q_pin.cell_type == "sky130_fd_sc_hd__dfxtp_1"
    # The second path proves a multi-cell ordered chain with its own totals.
    second = paths[1]
    assert second.startpoint == "_and0_"
    assert second.arrival_ns == 0.94
    assert second.slack_ns == 8.81


def test_parse_hold_report_distinguishes_min_analysis_and_violation():
    paths = timing_paths.parse_ordered_report(HOLD_REPORT, filename_hint="hold.topN")
    assert len(paths) == 1
    path = paths[0]
    assert path.analysis == "hold"
    assert path.corner == "fast"
    assert path.slack_ns == -0.13
    assert path.met is False


def test_parse_rejects_blocks_without_measured_data():
    assert timing_paths.parse_ordered_report("") == []
    assert timing_paths.parse_ordered_report("Startpoint: x\nnothing else\n") == []


# -- Artifact contract --------------------------------------------------------


def test_artifact_is_digest_bound_and_load_verified():
    netlist = fixture_netlist_artifact()
    artifact = timing_paths.build_timing_paths(
        {"setup.topN": REPORT_FIXTURE.read_text()},
        netlist_artifact=netlist, job_id="job-x", project="proj-x",
        clock_period_ns=10.0)
    artifact = timing_paths.load_timing_paths(artifact)
    identity = artifact["identity"]
    assert artifact["schema"] == "timing-paths/v1"
    assert identity["netlist_sha256"] == netlist["identity"]["netlist_sha256"]
    assert identity["gate_artifact_sha256"] == netlist["artifact_sha256"]
    assert identity["clock_period_ns"] == 10.0
    assert identity["sources"] == ["setup.topN"]
    assert identity["path_count"] == 2
    # Byte-stable rebuild.
    again = timing_paths.build_timing_paths(
        {"setup.topN": REPORT_FIXTURE.read_text()},
        netlist_artifact=netlist, job_id="job-x", project="proj-x",
        clock_period_ns=10.0)
    assert gate_netlist.canonical_bytes(artifact) == gate_netlist.canonical_bytes(again)
    # Tampering is rejected by the digest check.
    tampered = json.loads(gate_netlist.canonical_bytes(artifact))
    tampered["paths"][0]["slack_ns"] = 0.0
    with pytest.raises(timing_paths.TimingPathError, match="digest"):
        timing_paths.load_timing_paths(tampered)
    with pytest.raises(timing_paths.TimingPathError, match="unsupported"):
        timing_paths.load_timing_paths({**json.loads(gate_netlist.canonical_bytes(artifact)),
                                        "schema": "timing-paths/v2"})


def test_segments_map_to_gate_graph_ids_with_totals():
    netlist = fixture_netlist_artifact()
    artifact = timing_paths.load_timing_paths(timing_paths.build_timing_paths(
        {"setup.topN": REPORT_FIXTURE.read_text()},
        netlist_artifact=netlist, job_id="job-x"))
    first = artifact["paths"][0]
    assert first["mapping"]["partial"] is False
    assert first["mapping"]["unresolved"] == []
    by_name = {segment["name"]: segment for segment in first["segments"]
               if segment["kind"] == "net"}
    assert by_name["q[0]"]["graph_id"].startswith("net:counter8:")
    assert by_name["q[0]"]["kind"] == "net"
    q_pin = next(segment for segment in first["segments"]
                 if segment["name"] == "_ff0_/Q")
    assert q_pin["graph_id"] == "cell:_ff0_"
    port_segment = next(segment for segment in first["segments"]
                        if segment["kind"] == "port")
    assert port_segment["graph_id"] == "port:q"
    # Delay totals: the measured cumulative time of the last data segment
    # equals the reported data arrival time.
    last_data = max((segment for segment in first["segments"]
                     if segment["kind"] in ("cell_pin", "port")),
                    key=lambda segment: segment["time_ns"] or 0)
    assert last_data["time_ns"] == first["data_arrival_ns"]


def test_partial_mapping_is_explicit_and_lists_every_unresolved_segment():
    netlist = fixture_netlist_artifact()
    report = REPORT_FIXTURE.read_text().replace("_and0_", "_missing_cell_")
    artifact = timing_paths.load_timing_paths(timing_paths.build_timing_paths(
        {"setup.topN": report}, netlist_artifact=netlist, job_id="job-x"))
    partial = next(path for path in artifact["paths"]
                   if path["mapping"]["partial"])
    assert "_missing_cell_/A" in partial["mapping"]["unresolved"]
    assert "_missing_cell_/X" in partial["mapping"]["unresolved"]
    for segment in partial["segments"]:
        if segment["name"].startswith("_missing_cell_"):
            assert segment["graph_id"] is None
    assert artifact["identity"]["complete"] is False


def test_collect_timing_paths_from_build_dir(tmp_path):
    netlist = fixture_netlist_artifact()
    build = tmp_path / "counter8" / "job0" / "timing/0/reports/timing"
    build.mkdir(parents=True)
    (build / "setup.topN.rpt").write_text(REPORT_FIXTURE.read_text())
    scenarios = tmp_path / "counter8" / "job0" / "timing/0/reports/timing/scenarios/fast"
    scenarios.mkdir(parents=True)
    (scenarios / "hold.fast.topN.rpt").write_text(HOLD_REPORT)
    summary = timing_paths.collect_timing_paths(
        tmp_path, "counter8", netlist, job_id="job-x", clock_period_ns=10.0)
    assert summary["available"] is True
    artifact = summary.pop("artifact")
    assert summary["repository_sha256"] == hashlib.sha256(
        gate_netlist.canonical_bytes(artifact)).hexdigest()
    assert artifact["identity"]["sources"] == ["hold.fast.topN.fast", "setup.topN"]
    analyses = {path["analysis"] for path in artifact["paths"]}
    assert analyses == {"setup", "hold"}
    # No reports at all -> honest unavailable, never guessed paths.
    empty = timing_paths.collect_timing_paths(tmp_path / "elsewhere", "counter8", netlist)
    assert empty["available"] is False
    assert "no ordered timing-path reports" in empty["reason"]


# -- Job integration + REST contract ------------------------------------------


def _run_job_with_artifacts(tmp_path, monkeypatch, repo):
    """A finished synthesis job carrying real persisted netlist + paths."""
    monkeypatch.setattr(evidence, "repository", repo)
    written = tmp_path / "counter8.yosys.json"
    written.write_bytes(NETLIST_FIXTURE.read_bytes())
    meta = eda_tools._persist_gate_netlist(
        {"status": "available", "path": str(written)},
        job_id=None, project="proj-x", top_module="counter8",
        source_sha256="a" * 64, constraints_sha256=None,
        library="sky130_fd_sc_hd")
    assert meta["available"] is True
    build = tmp_path / "counter8" / "job0" / "timing/0/reports/timing"
    build.mkdir(parents=True)
    (build / "setup.topN.rpt").write_text(REPORT_FIXTURE.read_text())
    paths_meta = eda_tools._persist_timing_paths(
        tmp_path, "counter8", meta, job_id="job-live",
        project="proj-x", clock_period_ns=10.0)
    assert paths_meta["available"] is True, paths_meta

    manager = JobManager(tmp_path / "jobs.db", tmp_path / "jobs")

    async def scenario():
        async def runner(job):
            job.manifest = {"top_module": "counter8", "status": "success",
                            "gate_netlist": meta, "timing_paths": paths_meta}
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
    return manager, job_id, paths_meta


client = TestClient(app)


def test_timing_paths_endpoint_serves_mapping_and_rejects_digest_mismatch(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    manager, job_id, paths_meta = _run_job_with_artifacts(tmp_path, monkeypatch, repo)
    monkeypatch.setattr(jobs_mod, "manager", manager)

    response = client.get(f"/api/jobs/{job_id}/timing-paths")
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "timing-paths/v1"
    assert body["identity"]["path_count"] == 2
    assert body["identity"]["netlist_sha256"] == paths_meta["netlist_sha256"]
    first = body["paths"][0]
    assert first["mapping"]["partial"] is False
    resolved = [segment["graph_id"] for segment in first["segments"]
                if segment["graph_id"]]
    assert "cell:_ff0_" in resolved

    # Digest pinning: a request naming a different netlist is rejected.
    ok = client.get(f"/api/jobs/{job_id}/timing-paths",
                    params={"netlist_sha256": paths_meta["netlist_sha256"]})
    assert ok.status_code == 200
    stale = client.get(f"/api/jobs/{job_id}/timing-paths",
                       params={"netlist_sha256": "b" * 64})
    assert stale.status_code == 409
    assert client.get("/api/jobs/no-such-job/timing-paths").status_code == 404


def test_timing_paths_survive_backend_restart(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    manager, job_id, paths_meta = _run_job_with_artifacts(tmp_path, monkeypatch, repo)
    # A restarted backend: fresh manager (no live manifest) and fresh
    # evidence repository on the same durable root.
    monkeypatch.setattr(jobs_mod, "manager", JobManager(tmp_path / "jobs.db", tmp_path / "jobs"))
    monkeypatch.setattr(evidence, "repository", EvidenceRepository(tmp_path / "evidence"))
    response = client.get(f"/api/jobs/{job_id}/timing-paths")
    assert response.status_code == 200
    body = response.json()
    assert body["identity"]["path_count"] == 2
    assert "cell:_ff0_" in [
        segment["graph_id"] for segment in body["paths"][0]["segments"]]


def test_job_without_sta_reports_reports_unavailable(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    monkeypatch.setattr(evidence, "repository", repo)
    unavailable = eda_tools._persist_timing_paths(
        tmp_path, "counter8", {"available": False}, job_id="j",
        project=None, clock_period_ns=None)
    assert unavailable["available"] is False
    assert "no gate netlist artifact" in unavailable["reason"]

    manager = JobManager(tmp_path / "jobs.db", tmp_path / "jobs")

    async def scenario():
        async def runner(job):
            job.manifest = {"gate_netlist": {"available": False},
                            "timing_paths": unavailable}
            job.set_outcome("success")
            return "ok"

        job = manager.submit("synthesis", {}, runner=runner, design="counter8",
                             session="proj-x")
        await job.wait()
        return job.id

    try:
        job_id = asyncio.run(scenario())
    finally:
        asyncio.run(manager.stop())
    monkeypatch.setattr(jobs_mod, "manager", manager)
    response = client.get(f"/api/jobs/{job_id}/timing-paths")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "no gate netlist artifact" in body["reason"]
    assert "paths" not in body
