"""Focused C3 contract and parser-boundary regressions."""

from pathlib import Path

import pytest

import physical_layout
import timing_paths
from visualization_artifacts import seal


FIXTURE = Path(__file__).parent / "fixtures"


def test_timing_parser_rejects_oversized_and_non_text_input():
    with pytest.raises(timing_paths.TimingPathError, match="must be text"):
        timing_paths.parse_ordered_report(None)
    with pytest.raises(timing_paths.TimingPathError, match="size bound"):
        timing_paths.parse_ordered_report("x" * (timing_paths._MAX_REPORT_BYTES + 1))


def test_timing_builder_bounds_report_count_and_labels():
    with pytest.raises(timing_paths.TimingPathError, match="too many"):
        timing_paths.build_timing_paths(
            {str(index): "" for index in range(timing_paths._MAX_REPORTS + 1)},
            netlist_artifact={})
    with pytest.raises(timing_paths.TimingPathError, match="labels"):
        timing_paths.build_timing_paths({"": ""}, netlist_artifact={})


def test_timing_loader_rejects_oversized_segment_collection():
    artifact = seal({
        "schema": timing_paths.SCHEMA_VERSION,
        "identity": {},
        "paths": [{
            "segments": [{}] * (timing_paths._MAX_SEGMENTS + 1),
            "mapping": {"unresolved": []},
        }],
    })
    with pytest.raises(timing_paths.TimingPathError, match="segments"):
        timing_paths.load_timing_paths(artifact)


def test_def_parser_rejects_duplicate_component_and_pin_ids():
    source = (FIXTURE / "counter8.def").read_text()
    duplicate_component = source.replace(
        "- _and0_ sky130_fd_sc_hd__and2_0 + PLACED ( 1380 0 ) N ;",
        "- _and0_ sky130_fd_sc_hd__and2_0 + PLACED ( 1380 0 ) N ;\n"
        "    - _and0_ sky130_fd_sc_hd__and2_0 + PLACED ( 1840 0 ) N ;",
    )
    with pytest.raises(physical_layout.LayoutError, match="duplicate DEF component"):
        physical_layout.parse_def(duplicate_component)

    duplicate_pin = source.replace(
        "- q ( * OUTPUT ) + NET q + PLACED ( 9200 1360 ) N ;",
        "- q ( * OUTPUT ) + NET q + PLACED ( 9200 1360 ) N ;\n"
        "    - q ( * OUTPUT ) + NET q + PLACED ( 9200 1360 ) N ;",
    )
    with pytest.raises(physical_layout.LayoutError, match="duplicate DEF pin"):
        physical_layout.parse_def(duplicate_pin)


def test_layout_loader_rejects_duplicate_component_after_digest_check():
    view = physical_layout.build_layout_view((FIXTURE / "counter8.def").read_text())
    view["components"].append(dict(view["components"][0]))
    tampered = seal(view)
    with pytest.raises(physical_layout.LayoutError, match="duplicate component"):
        physical_layout.load_layout_view(tampered)
