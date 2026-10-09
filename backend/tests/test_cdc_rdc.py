"""Focused structural CDC/RDC fixtures."""

from datetime import date

from cdc_rdc import CdcWaiver, CdcWaiverScope, analyze_cdc_rdc


def _netlist(cells, *, reset=False):
    ports = {
        "clk_a": {"direction": "input", "bits": [2]},
        "clk_b": {"direction": "input", "bits": [3]},
        "data": {"direction": "input", "bits": [4]},
    }
    if reset:
        ports["rst_n"] = {"direction": "input", "bits": [9]}
    return {"modules": {"top": {"ports": ports, "cells": cells,
                                  "netnames": {name: {"bits": bits} for name, bits in {
                                      "clk_a": [2], "clk_b": [3], "data": [4],
                                      "rst_n": [9], "src_q": [5], "sync_q": [6],
                                  }.items() if name != "rst_n" or reset}}}}


def test_two_flop_synchronizer_is_recognized():
    report = analyze_cdc_rdc(_netlist({
        "src": {"type": "$dff", "connections": {"C": [2], "D": [4], "Q": [5]}},
        "sync1": {"type": "$dff", "connections": {"C": [3], "D": [5], "Q": [6]}},
        "sync2": {"type": "$dff", "connections": {"C": [3], "D": [6], "Q": [7]}},
    }))
    assert report.status == "pass"
    assert report.synchronizers[0]["cells"] == ["sync1", "sync2"]
    assert report.findings == []


def test_direct_and_multibit_crossings_are_blocking():
    report = analyze_cdc_rdc(_netlist({
        "src": {"type": "$dff", "connections": {"C": [2], "D": [4, 4], "Q": [5, 8]}},
        "sink": {"type": "$dff", "connections": {"C": [3], "D": [5, 8], "Q": [6, 7]}},
    }))
    assert report.status == "fail"
    assert any(item.rule_id == "CDC-MULTIBIT" for item in report.findings)


def test_async_reset_release_chain_and_missing_chain():
    safe = analyze_cdc_rdc(_netlist({
        "r1": {"type": "$adff", "connections": {"C": [3], "D": [1], "Q": [5], "ARST": [9]}},
        "r2": {"type": "$adff", "connections": {"C": [3], "D": [5], "Q": [6], "ARST": [9]}},
    }, reset=True))
    assert safe.status == "pass"
    assert any(item["kind"] == "async-assert-sync-release" for item in safe.synchronizers)
    unsafe = analyze_cdc_rdc(_netlist({
        "r": {"type": "$adff", "connections": {"C": [3], "D": [4], "Q": [5], "ARST": [9]}},
    }, reset=True))
    assert unsafe.status == "fail"
    assert unsafe.findings[0].rule_id == "RDC-ASYNC-RESET"


def test_expiring_waiver_and_artifact(tmp_path):
    waiver = CdcWaiver(id="W-RDC", reason="legacy wrapper", author="alice",
                       scope=CdcWaiverScope(rule_id="CDC-UNSYNC", top_module="top"),
                       expires=date(2099, 1, 1))
    report = analyze_cdc_rdc(_netlist({
        "src": {"type": "$dff", "connections": {"C": [2], "D": [4], "Q": [5]}},
        "sink": {"type": "$dff", "connections": {"C": [3], "D": [5], "Q": [6]}},
    }), waivers=[waiver], persist_dir=tmp_path)
    assert report.status == "pass"
    assert report.waivers_applied == ["W-RDC"]
    assert report.artifact_path


def test_missing_yosys_is_inconclusive(monkeypatch):
    monkeypatch.setattr("cdc_rdc.shutil.which", lambda name: None)
    report = analyze_cdc_rdc("module top(input clk); endmodule", top_module="top")
    assert report.status == "inconclusive"
    assert report.tool_available is False
