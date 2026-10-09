"""Phase G4 — indexed layout view from the exact post-P&R DEF.

The fixture DEF (``tests/fixtures/counter8.def``) carries the same component
names as the counter8 gate-netlist fixture, so cell-level cross-probing
between the schematic and the physical view is proven against identical
identities.
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
import physical_layout
from evidence import EvidenceRepository
from jobs import JobManager
from main import app

FIXTURE = Path(__file__).parent / "fixtures"
NETLIST_FIXTURE = FIXTURE / "counter8.yosys.json"
DEF_FIXTURE = FIXTURE / "counter8.def"


def fixture_netlist_artifact():
    data = json.loads(NETLIST_FIXTURE.read_text())
    return gate_netlist.load_gate_netlist(json.loads(
        gate_netlist.canonical_bytes(
            gate_netlist.build_gate_netlist(data, top_module="counter8",
                                            netlist_sha256="e" * 64))))


# -- DEF parsing --------------------------------------------------------------


def test_parse_def_extracts_die_rows_components_pins():
    parsed = physical_layout.parse_def(DEF_FIXTURE.read_text())
    assert parsed["design"] == "counter8"
    assert parsed["units"] == 1000
    assert parsed["die"] == {"llx": 0, "lly": 0, "urx": 9200, "ury": 6100}
    assert len(parsed["rows"]) == 2
    assert parsed["rows"][0]["step_x"] == 460
    placed = {component["name"]: component for component in parsed["components"]}
    assert placed["_ff0_"]["x"] == 460 and placed["_ff0_"]["y"] == 0
    assert placed["_slice_"]["orientation"] == "N"
    assert parsed["unplaced_count"] == 1  # _macro_ram_ has no PLACED entry
    pins = {pin["name"]: pin for pin in parsed["pins"]}
    assert pins["clk"]["direction"] == "INPUT"
    assert pins["clk"]["net"] == "clk"
    assert pins["q"]["x"] == 9200
    # The row pitch (2720) and step give the nominal footprint.
    view = physical_layout.build_layout_view(DEF_FIXTURE.read_text())
    assert view["nominal_footprint"] == {"w": 460, "h": 2720}


def test_parse_def_rejects_empty_documents():
    with pytest.raises(physical_layout.LayoutError):
        physical_layout.parse_def("")
    with pytest.raises(physical_layout.LayoutError):
        physical_layout.parse_def("nothing recognizable")


# -- Artifact contract --------------------------------------------------------


def test_layout_view_is_digest_bound_maps_cells_and_is_byte_stable():
    netlist = fixture_netlist_artifact()
    first = physical_layout.build_layout_view(
        DEF_FIXTURE.read_text(), netlist_artifact=netlist,
        job_id="j-live", clock_period_ns=10.0)
    second = physical_layout.build_layout_view(
        DEF_FIXTURE.read_text(), netlist_artifact=netlist,
        job_id="j-live", clock_period_ns=10.0)
    assert gate_netlist.canonical_bytes(first) == gate_netlist.canonical_bytes(second)
    view = physical_layout.load_layout_view(json.loads(
        gate_netlist.canonical_bytes(first)))
    identity = view["identity"]
    assert identity["netlist_sha256"] == netlist["identity"]["netlist_sha256"]
    assert identity["gate_artifact_sha256"] == netlist["artifact_sha256"]
    assert identity["def_sha256"] == hashlib.sha256(
        DEF_FIXTURE.read_bytes()).hexdigest()
    # Every placed component maps to its gate-graph cell id.
    by_name = {component["name"]: component for component in view["components"]}
    assert by_name["_ff0_"]["graph_id"] == "cell:_ff0_"
    assert by_name["_slice_"]["graph_id"] == "cell:_slice_"
    assert by_name["_and0_"]["graph_id"] == "cell:_and0_"
    assert view["completeness"]["mapping_partial"] is False
    assert view["completeness"]["nominal_cell_footprint"] is True
    # Tampering is rejected by the digest check.
    tampered = json.loads(gate_netlist.canonical_bytes(view))
    tampered["components"][0]["x"] = 99999
    with pytest.raises(physical_layout.LayoutError, match="digest"):
        physical_layout.load_layout_view(tampered)
    with pytest.raises(physical_layout.LayoutError, match="unsupported"):
        physical_layout.load_layout_view({**json.loads(gate_netlist.canonical_bytes(view)),
                                          "schema": "layout-view/v2"})


def test_unmapped_components_are_reported_not_guessed():
    netlist = fixture_netlist_artifact()
    view = physical_layout.build_layout_view(
        DEF_FIXTURE.read_text(), netlist_artifact=netlist, job_id="j-live")
    unmapped = [component for component in view["components"]
               if component["graph_id"] is None]
    assert unmapped == []
    # A DEF with a component the netlist does not know reports partial
    # mapping and leaves that component's graph_id null.
    def_text = DEF_FIXTURE.read_text().replace(
        "- _and0_ sky130_fd_sc_hd__and2_0 + PLACED ( 1380 0 ) N ;",
        "- _and0_ sky130_fd_sc_hd__and2_0 + PLACED ( 1380 0 ) N ;\n"
        "    - _stray_ sky130_fd_sc_hd__buf_1 + PLACED ( 1840 0 ) N ;")
    view = physical_layout.build_layout_view(
        def_text, netlist_artifact=netlist, job_id="j-live")
    stray = next(c for c in view["components"] if c["name"] == "_stray_")
    assert stray["graph_id"] is None
    assert view["completeness"]["mapping_partial"] is True


def test_collect_layout_view_from_build_dir(tmp_path):
    netlist = fixture_netlist_artifact()
    outputs = tmp_path / "counter8" / "job0" / "route" / "0" / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "counter8.def").write_text(DEF_FIXTURE.read_text())
    summary = physical_layout.collect_layout_view(
        tmp_path, "counter8", netlist, job_id="j-live", clock_period_ns=10.0)
    assert summary["available"] is True
    artifact = summary.pop("artifact")
    assert summary["repository_sha256"] == hashlib.sha256(
        gate_netlist.canonical_bytes(artifact)).hexdigest()
    # A screening build with no DEF is honestly unavailable.
    empty = physical_layout.collect_layout_view(tmp_path / "elsewhere", "counter8", netlist)
    assert empty["available"] is False
    assert "no physical implementation" in empty["reason"]


# -- Job integration + REST contract ------------------------------------------


def _run_job_with_layout(tmp_path, monkeypatch, repo, *, with_def=True):
    monkeypatch.setattr(evidence, "repository", repo)
    written = tmp_path / "counter8.yosys.json"
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_bytes(NETLIST_FIXTURE.read_bytes())
    meta = eda_tools._persist_gate_netlist(
        {"status": "available", "path": str(written)},
        job_id=None, project="proj-x", top_module="counter8",
        source_sha256="a" * 64, constraints_sha256=None,
        library="sky130_fd_sc_hd")
    if with_def:
        outputs = tmp_path / "counter8" / "job0" / "route" / "0" / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        (outputs / "counter8.def").write_text(DEF_FIXTURE.read_text())
    layout_meta = eda_tools._persist_layout_view(
        tmp_path, "counter8", meta, job_id="j-live",
        project="proj-x", clock_period_ns=10.0)
    manager = JobManager(tmp_path / "jobs.db", tmp_path / "jobs")

    async def scenario():
        async def runner(job):
            job.manifest = {"top_module": "counter8", "status": "success",
                            "gate_netlist": meta, "layout_view": layout_meta}
            job.set_outcome("success")
            return "ok"

        job = manager.submit("synthesis", {}, runner=runner,
                             design="counter8", session="proj-x")
        await job.wait()
        return job.id

    try:
        job_id = asyncio.run(scenario())
    finally:
        asyncio.run(manager.stop())
    return manager, job_id, layout_meta


client = TestClient(app)


def test_layout_endpoint_serves_view_and_rejects_digest_mismatch(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    manager, job_id, layout_meta = _run_job_with_layout(tmp_path, monkeypatch, repo)
    monkeypatch.setattr(jobs_mod, "manager", manager)

    response = client.get(f"/api/jobs/{job_id}/layout")
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "layout-view/v1"
    assert body["identity"]["design"] == "counter8"
    assert body["die"]["urx"] == 9200
    by_name = {component["name"]: component for component in body["components"]}
    assert by_name["_ff0_"]["graph_id"] == "cell:_ff0_"

    ok = client.get(f"/api/jobs/{job_id}/layout",
                    params={"netlist_sha256": layout_meta["netlist_sha256"]})
    assert ok.status_code == 200
    stale = client.get(f"/api/jobs/{job_id}/layout",
                       params={"netlist_sha256": "b" * 64})
    assert stale.status_code == 409
    assert client.get("/api/jobs/no-such-job/layout").status_code == 404


def test_layout_survives_backend_restart_and_screening_reports_unavailable(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    manager, job_id, layout_meta = _run_job_with_layout(tmp_path, monkeypatch, repo)
    # Restart: fresh manager (no live manifest) and fresh repository instance.
    monkeypatch.setattr(jobs_mod, "manager", JobManager(tmp_path / "jobs.db", tmp_path / "jobs"))
    monkeypatch.setattr(evidence, "repository", EvidenceRepository(tmp_path / "evidence"))
    response = client.get(f"/api/jobs/{job_id}/layout")
    assert response.status_code == 200
    assert response.json()["die"]["urx"] == 9200

    # A screening job (no DEF) is honestly unavailable, with no layout at all.
    manager2, job2, layout_meta2 = _run_job_with_layout(
        tmp_path / "screen", monkeypatch, EvidenceRepository(tmp_path / "screen-evidence"),
        with_def=False)
    monkeypatch.setattr(jobs_mod, "manager", manager2)
    assert layout_meta2["available"] is False
    assert "no physical implementation" in layout_meta2["reason"]
    response2 = client.get(f"/api/jobs/{job2}/layout")
    assert response2.status_code == 200
    body = response2.json()
    assert body["available"] is False
    assert "components" not in body
