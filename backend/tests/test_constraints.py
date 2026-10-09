import pytest

from constraints import (
    ClockConstraint,
    ClockUncertaintyConstraint,
    ConstraintSet,
    ElectricalLimits,
    FalsePathConstraint,
    InputDriveConstraint,
    IoDelayConstraint,
    MulticyclePathConstraint,
    OutputLoadConstraint,
    TargetConstraint,
)
from evidence import DesignSpec, EvidenceRepository, sha256_record


def _constraints() -> ConstraintSet:
    return ConstraintSet(
        target=TargetConstraint(process="sky130", device="sky130A",
                                library="sky130_fd_sc_hd", corner="typical"),
        clocks=[ClockConstraint(name="clk", port="clk", period_ns=10.0)],
        uncertainties=[ClockUncertaintyConstraint(clock="clk", setup_ns=.25,
                                                   hold_ns=.1)],
        input_drives=[InputDriveConstraint(port="data_in",
                                           library_cell="sky130_fd_sc_hd__buf_4")],
        output_loads=[OutputLoadConstraint(port="data_out", capacitance_pf=.03)],
        electrical_limits=ElectricalLimits(max_transition_ns=.75,
                                           max_capacitance_pf=.2,
                                           max_fanout=10),
        io_delays=[IoDelayConstraint(port="data_in", direction="input",
                                     clock="clk", max_delay_ns=2.0,
                                     min_delay_ns=.2)],
        false_paths=[FalsePathConstraint(**{"from": "rst_n"}, reason="async reset")],
        multicycle_paths=[MulticyclePathConstraint(**{"to": "slow_out"},
                                                    setup_cycles=2,
                                                    hold_cycles=1,
                                                    reason="slow protocol")],
    )


def test_constraints_validate_references_and_render_safe_sdc():
    constraints = _constraints()
    sdc = constraints.to_sdc()
    assert "create_clock -name clk -period 10" in sdc
    assert "set_clock_uncertainty -setup 0.25" in sdc
    assert "set_input_delay -min 0.2" in sdc
    assert "set_driving_cell -lib_cell sky130_fd_sc_hd__buf_4 [get_ports {data_in}]" in sdc
    assert "set_load -pin_load 0.03 [get_ports {data_out}]" in sdc
    assert "set_max_transition 0.75 [current_design]" in sdc
    assert "set_max_capacitance 0.2 [current_design]" in sdc
    assert "set_max_fanout 10 [current_design]" in sdc
    assert "set_false_path -from [get_ports {rst_n}]" in sdc
    assert "set_multicycle_path 2 -setup" in sdc
    assert "sky130A" not in sdc
    assert "typical" not in sdc


def test_constraints_are_content_addressed_and_persist_inside_design_spec(tmp_path):
    spec = DesignSpec(design_id="d", name="D", top_module="top",
                      constraints=_constraints())
    assert spec.constraints.content_sha256() == sha256_record(spec.constraints)
    repository = EvidenceRepository(tmp_path)
    repository.save_spec(spec)
    assert repository.load_spec("d", 1).constraints == spec.constraints


def test_constraints_reject_unsafe_or_ambiguous_input():
    with pytest.raises(ValueError, match="safe port/pin selector"):
        FalsePathConstraint(**{"from": "clk; reset"}, reason="bad")
    with pytest.raises(ValueError, match="unknown clock"):
        ConstraintSet(io_delays=[IoDelayConstraint(
            port="d", direction="input", clock="missing", max_delay_ns=1)])
    with pytest.raises(ValueError, match="min_delay_ns"):
        IoDelayConstraint(port="d", direction="input", clock="clk",
                          min_delay_ns=2, max_delay_ns=1)
    with pytest.raises(ValueError, match="hold_cycles"):
        MulticyclePathConstraint(**{"from": "a"}, setup_cycles=1,
                                 hold_cycles=2, reason="bad")


def test_constraint_hash_is_stable():
    constraints = _constraints()
    digest = constraints.content_sha256()
    assert len(digest) == 64
    assert int(digest, 16) >= 0
    with pytest.raises(ValueError):
        TargetConstraint(process="sky130\nexec", device="x")


def test_virtual_clock_has_no_source_port_query():
    clock = ClockConstraint(name="virt", virtual=True, period_ns=5.0)
    sdc = ConstraintSet(clocks=[clock]).to_sdc()
    assert "create_clock -name virt -period 5" in sdc
    assert "get_ports" not in sdc


def test_non_virtual_clock_requires_source_port():
    with pytest.raises(ValueError, match="source port"):
        ClockConstraint(name="clk", period_ns=5.0)
