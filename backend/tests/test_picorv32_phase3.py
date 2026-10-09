from picorv32_phase3 import INPUT_PORTS, OUTPUT_PORTS, reviewed_constraints


def test_reviewed_picorv32_constraints_are_explicit_and_50mhz():
    constraints = reviewed_constraints()
    assert constraints.clocks[0].port == "clk"
    assert constraints.clocks[0].period_ns == 20.0
    assert constraints.false_paths[0].from_path == "resetn"
    assert "clk" not in INPUT_PORTS
    assert "resetn" not in INPUT_PORTS
    assert "mem_ready" in INPUT_PORTS
    assert "trace_data[*]" in OUTPUT_PORTS
    sdc = constraints.to_sdc()
    assert "create_clock -name clk -period 20" in sdc
    assert "set_max_transition 1.5" in sdc
    assert "set_max_capacitance 0.2" in sdc
    assert "set_max_fanout 32" in sdc
