from pathlib import Path

import pytest

from physical_evidence import PhysicalEvidenceError, collect_physical_evidence


def _reports(tmp_path: Path, *, drv: str = "", floating: str = "",
             overdriven: str = "", unconstrained: str = "") -> dict[str, Path]:
    values = {"drv_violators": drv, "floating_nets": floating,
              "overdriven_nets": overdriven,
              "unconstrained_endpoints": unconstrained}
    result = {}
    for name, content in values.items():
        path = tmp_path / f"{name}.rpt"
        path.write_text(content)
        result[name] = path
    return result


def test_parses_driver_categories_and_hashes_all_reports(tmp_path):
    reports = _reports(tmp_path, drv="""max slew

Pin                                    Limit    Slew   Slack
------------------------------------------------------------
u1/A                                   1.50     2.10   -0.60 (VIOLATED)

max capacitance
Pin                                    Limit    Cap    Slack
------------------------------------------------------------
u2/Y                                   0.20     0.30   -0.10 (VIOLATED)

max fanout
Pin                                    Limit    Fanout Slack
------------------------------------------------------------
u3/Z                                   4        7      -3 (VIOLATED)
u4/Z                                   32       33        (VIOLATED)
""")
    evidence = collect_physical_evidence(reports)
    assert evidence.status == "violations"
    assert [(v.category, v.object_name) for v in evidence.violations] == [
        ("max_slew", "u1/A"), ("max_capacitance", "u2/Y"),
        ("max_fanout", "u3/Z"), ("max_fanout", "u4/Z")]
    assert evidence.violations[-1].slack == -1
    assert all(len(item.sha256) == 64 for item in evidence.reports)
    assert len(evidence.report_digest) == 64


def test_empty_check_reports_are_clean(tmp_path):
    evidence = collect_physical_evidence(_reports(tmp_path))
    assert evidence.clean
    assert evidence.status == "clean"
    assert evidence.unresolved_violation_count == 0


def test_floating_and_unconstrained_entries_fail_clean_gate(tmp_path):
    evidence = collect_physical_evidence(
        _reports(tmp_path, floating="net_a\n", unconstrained="reg_q[0]\n"))
    assert evidence.status == "violations"
    assert evidence.floating_nets == ("net_a",)
    assert evidence.unconstrained_endpoints == ("reg_q[0]",)
    assert not evidence.clean


def test_opensta_unconstrained_path_counts_one_endpoint_not_detail_lines(tmp_path):
    report = """Startpoint: cfg_i[1] (input port clocked by clk_i)
Endpoint: ANTENNA_12/DIODE (internal pin)
Path Group: unconstrained
Path Type: max

Fanout Cap Slew Delay Time Description
--------------------------------------
  4.00 4.00 input external delay
  cfg_i[1] (net)
(Path is unconstrained)
"""
    evidence = collect_physical_evidence(_reports(tmp_path, unconstrained=report))
    assert evidence.unconstrained_endpoints == ("ANTENNA_12/DIODE",)
    assert evidence.status == "violations"


def test_detailed_unconstrained_report_overrides_empty_compact_metric(tmp_path):
    detailed = tmp_path / "unconstrained.rpt"
    detailed.write_text("""Startpoint: data_gnt_i (input port clocked by clk_i)
Endpoint: u_dead/A (internal pin)
Path Group: unconstrained
Path Type: max
(Path is unconstrained)
""")
    evidence = collect_physical_evidence(
        _reports(tmp_path), additional_unconstrained_reports=[detailed])
    assert evidence.status == "violations"
    assert evidence.unconstrained_endpoints == ("u_dead/A",)
    assert evidence.reports[-1].category == "unconstrained_detail_0"


def test_malformed_opensta_unconstrained_block_fails_closed(tmp_path):
    reports = _reports(tmp_path, unconstrained="Startpoint: cfg_i[0] (input port)\n")
    with pytest.raises(PhysicalEvidenceError, match="unmatched Startpoint/Endpoint"):
        collect_physical_evidence(reports)


def test_exact_object_waiver_is_allowed_but_global_is_rejected(tmp_path):
    reports = _reports(tmp_path, drv="""max slew
Pin                                    Limit    Slew   Slack
------------------------------------------------------------
u1/A                                   1.50     2.10   -0.60 (VIOLATED)
""")
    evidence = collect_physical_evidence(
        reports, waivers=[{"category": "max_slew", "object": "u1/A",
                           "reason": "known library black-box boundary"}])
    assert evidence.status == "clean"
    assert evidence.violations[0].status == "waived"
    with pytest.raises(PhysicalEvidenceError, match="wildcards"):
        collect_physical_evidence(
            reports, waivers=[{"category": "max_slew", "object": "*",
                               "reason": "ignore all"}])


def test_missing_or_malformed_reports_fail_closed(tmp_path):
    reports = _reports(tmp_path)
    reports.pop("floating_nets")
    with pytest.raises(PhysicalEvidenceError, match="missing required"):
        collect_physical_evidence(reports)
    malformed = _reports(tmp_path, drv="max slew\nthis is not a report row\n")
    with pytest.raises(PhysicalEvidenceError, match="malformed"):
        collect_physical_evidence(malformed)


def test_unmatched_waiver_cannot_hide_a_different_object(tmp_path):
    reports = _reports(tmp_path, drv="""max slew
Pin                                    Limit    Slew   Slack
------------------------------------------------------------
u1/A                                   1.50     2.10   -0.60 (VIOLATED)
""")
    with pytest.raises(PhysicalEvidenceError, match="does not match"):
        collect_physical_evidence(
            reports, waivers=[{"category": "max_slew", "object": "u2/A",
                               "reason": "not the reported object"}])



def test_detailed_unconstrained_no_paths_found_is_empty(tmp_path):
    reports = _reports(tmp_path)
    reports["unconstrained_endpoints"].write_text("")
    detailed = tmp_path / "unconstrained.rpt"
    detailed.write_text("No paths found.\n")
    evidence = collect_physical_evidence(
        reports, additional_unconstrained_reports=[detailed])
    assert evidence.unconstrained_endpoints == ()
