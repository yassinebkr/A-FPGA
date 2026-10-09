import json

from timing_diagnosis import (
    normalize_timing_metrics,
    parse_fmax_report,
    parse_timing_report,
    parse_worst_slack_report,
    read_timing_diagnosis,
)


SETUP_REPORT = """Startpoint: pe0/product_reg[0] (rising edge-triggered flip-flop clocked by clk)
Endpoint: pe0/acc_reg[0]/D (rising edge-triggered flip-flop clocked by clk)
Path Group: clk
Path Type: max
Corner: slow

                                  1.25 data arrival time
                                  20.00 data required time
                                  -2.43 slack (VIOLATED)
"""


def test_parse_opensta_path_exposes_critical_path_fields():
    paths = parse_timing_report(SETUP_REPORT)
    assert len(paths) == 1
    path = paths[0]
    assert path.startpoint == "pe0/product_reg[0]"
    assert path.endpoint == "pe0/acc_reg[0]/D"
    assert path.analysis_type == "setup"
    assert path.path_type == "max"
    assert path.path_group == "clk"
    assert path.clock == "clk"
    assert path.slack_ns == -2.43
    assert path.delay_ns == 1.25


def test_parse_hold_and_worst_slack_reports():
    assert parse_worst_slack_report("worst slack max -2.43", "setup")["setup_slack_ns"] == -2.43
    assert parse_worst_slack_report("worst slack min 0.08", "hold")["hold_slack_ns"] == 0.08
    hold = parse_timing_report(SETUP_REPORT.replace("Path Type: max", "Path Type: min")
                               .replace("-2.43 slack (VIOLATED)", "0.08 slack (MET)"), "hold")
    assert hold[0].analysis_type == "hold"
    assert hold[0].slack_ns == 0.08


def test_normalize_manifest_metrics_and_units():
    result = normalize_timing_metrics({
        "setupslack": "-2.43 ns",
        "holdslack": 80,
        "fmax": "50_000_000 Hz",
        "period": "20 ns",
    })
    assert result == {
        "setup_slack_ns": -2.43,
        "hold_slack_ns": 80.0,
        "fmax_mhz": 50.0,
        "clock_period_ns": 20.0,
    }


def test_parse_fmax_report_supports_table_and_summary_forms():
    report = """clock                                period   min_period     fmax_mhz  registers
clk                                20.0000       19.8000          50.51          8
"""
    assert parse_fmax_report(report) == {
        "clock": "clk", "clock_period_ns": 20.0, "fmax_mhz": 50.51,
    }
    assert parse_fmax_report("clk fmax = 62.5 MHz") ["fmax_mhz"] == 62.5


def test_read_timing_diagnosis_assembles_reports_and_manifest(tmp_path):
    root = tmp_path / "counter" / "job0" / "timing" / "0" / "reports"
    (root / "timing").mkdir(parents=True)
    (root / "clocks").mkdir()
    (root / "timing" / "setup.topN.rpt").write_text(SETUP_REPORT)
    (root / "timing" / "worst_slack.setup.rpt").write_text("worst slack max -2.43\n")
    (root / "clocks" / "fmax.rpt").write_text(
        "clock period min_period fmax_mhz registers\nclk 20.0 20.0 50.0 4\n")

    result = read_timing_diagnosis(tmp_path / "counter", "counter",
                                   {"setupslack": "-2.43 ns"})
    assert result["status"] == "available"
    assert result["setup_slack_ns"] == -2.43
    assert result["fmax_mhz"] == 50.0
    assert result["clock_period_ns"] == 20.0
    assert result["clock"] == "clk"
    assert result["paths"][0]["endpoint"].startswith("pe0/acc_reg")
    assert any(item.endswith("setup.topN.rpt") for item in result["source_files"])
    # The public result is safe to send as a tool-result JSON line.
    json.dumps(result)


def test_missing_or_malformed_reports_are_inconclusive(tmp_path):
    result = read_timing_diagnosis(tmp_path, "counter", {"setupslack": "not-a-number"})
    assert result["status"] == "unavailable"
    assert result["paths"] == []

