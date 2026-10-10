"""Tests for the read-only C6 existing-build qualification CLI."""

import json
from pathlib import Path

from qualify_existing_build import qualify_existing_build


FIXTURE = Path(__file__).parent / "fixtures"


def _fixture_build(tmp_path: Path, *, with_def: bool = True) -> Path:
    root = tmp_path / "build"
    netlist_dir = root / "counter8" / "job0" / "synthesis" / "0" / "outputs"
    netlist_dir.mkdir(parents=True)
    raw = json.loads((FIXTURE / "counter8.yosys.json").read_text())
    (netlist_dir / "counter8.netlist.json").write_text(json.dumps(raw))

    timing_dir = root / "counter8" / "job0" / "timing" / "0" / "reports" / "timing"
    timing_dir.mkdir(parents=True)
    (timing_dir / "setup.topN.rpt").write_text(
        (FIXTURE / "timing.setup.topN.rpt").read_text())
    if with_def:
        def_dir = root / "counter8" / "job0" / "route" / "0" / "outputs"
        def_dir.mkdir(parents=True)
        (def_dir / "counter8.def").write_text((FIXTURE / "counter8.def").read_text())
    return root


def test_existing_build_qualification_uses_all_production_collectors(tmp_path):
    root = _fixture_build(tmp_path)
    report = qualify_existing_build(root, "counter8", job_id="job-x")
    assert report["read_only"] is True
    assert report["eda_invoked"] is False
    assert report["model_invoked"] is False
    assert report["netlist"]["status"] == "available"
    assert report["netlist"]["validated"] is True
    assert report["timing"]["status"] == "available"
    assert report["timing"]["validated"] is True
    assert report["layout"]["status"] == "available"
    assert report["layout"]["validated"] is True
    assert report["qualification"]["status"] == "passed"
    assert report["netlist"]["path"].endswith("counter8.netlist.json")


def test_existing_build_reports_layout_unavailable_without_fabricating_it(tmp_path):
    root = _fixture_build(tmp_path, with_def=False)
    report = qualify_existing_build(root, "counter8")
    assert report["netlist"]["status"] == "available"
    assert report["timing"]["status"] == "available"
    assert report["layout"]["status"] == "unavailable"
    assert report["layout"]["validated"] is False
    assert "no physical implementation" in report["layout"]["reason"]
    assert report["qualification"]["status"] == "partial"


def test_existing_build_without_netlist_remains_honest(tmp_path):
    root = tmp_path / "build"
    (root / "counter8" / "job0").mkdir(parents=True)
    report = qualify_existing_build(root, "counter8")
    assert report["netlist"]["status"] == "unavailable"
    assert report["timing"]["status"] == "unavailable"
    assert report["layout"]["status"] == "unavailable"
    assert report["qualification"]["status"] == "partial"
