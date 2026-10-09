"""Tests for the deterministic post-sv2v adapter transforms."""
import hashlib

import pytest

from sv2v_adapter import (
    AdapterResult,
    Sv2vAdapterError,
    apply_adapter,
    apply_adapter_idempotent,
)


def test_no_2d_arrays_is_noop():
    text = "module top(input wire a, output wire y);\nassign y = a;\nendmodule\n"
    result = apply_adapter(text)
    assert result.output_text == text
    assert result.transforms_applied == []
    assert result.source_sha256 == result.output_sha256


def test_expands_2d_array_port_assign():
    text = """module top;
  reg [31:0] stage_counters [0:STAGES - 1][0:9];
  output wire [319:0] ext_counters;
  assign ext_counters = stage_counters[STAGES - 1];
endmodule
"""
    result = apply_adapter(text)
    assert len(result.transforms_applied) == 1
    t = result.transforms_applied[0]
    assert t.name == "expand_2d_array_port_assign"
    assert "stage_counters[STAGES - 1][9]" in result.output_text
    assert "stage_counters[STAGES - 1][0]" in result.output_text
    assert "{" in result.output_text
    assert "assign ext_counters = {" in result.output_text


def test_expands_2d_array_stage_copy():
    text = """module top;
  reg [31:0] stage_counters [0:STAGES - 1][0:9];
  always @(posedge clk) begin
    stage_counters[i] <= stage_counters[i - 1];
  end
endmodule
"""
    result = apply_adapter(text)
    assert len(result.transforms_applied) == 10
    for j in range(10):
        assert f"stage_counters[i][{j}] <= stage_counters[i - 1][{j}];" in result.output_text
    assert "stage_counters[i] <=" not in result.output_text


def test_adapter_is_idempotent():
    text = """module top;
  reg [31:0] stage_counters [0:STAGES - 1][0:9];
  output wire [319:0] ext_counters;
  assign ext_counters = stage_counters[STAGES - 1];
  always @(posedge clk) begin
    stage_counters[i] <= stage_counters[i - 1];
  end
endmodule
"""
    result = apply_adapter_idempotent(text)
    assert result.transforms_applied
    second = apply_adapter(result.output_text)
    assert second.transforms_applied == []
    assert second.output_sha256 == result.output_sha256


def test_adapter_preserves_non_array_assignments():
    text = """module top;
  reg [31:0] data [0:7];
  output wire [31:0] out;
  assign out = data[3];
endmodule
"""
    result = apply_adapter(text)
    assert result.transforms_applied == []
    assert result.output_text == text


def test_adapter_report_schema():
    text = """module top;
  reg [31:0] stage_counters [0:STAGES - 1][0:9];
  output wire [319:0] ext_counters;
  assign ext_counters = stage_counters[STAGES - 1];
endmodule
"""
    result = apply_adapter(text)
    report = result.report()
    assert report["schema"] == "sv2v-adapter-report/v1"
    assert report["source_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert report["output_sha256"] == hashlib.sha256(result.output_text.encode()).hexdigest()
    assert report["transform_count"] == 1
    assert report["transforms_applied"][0]["name"] == "expand_2d_array_port_assign"


def test_multiple_2d_arrays():
    text = """module top;
  reg [31:0] counters_lo [0:STAGES - 1][0:9];
  reg [31:0] counters_hi [0:STAGES - 1][0:9];
  output wire [319:0] ext_lo;
  output wire [319:0] ext_hi;
  assign ext_lo = counters_lo[STAGES - 1];
  assign ext_hi = counters_hi[STAGES - 1];
endmodule
"""
    result = apply_adapter(text)
    assert len(result.transforms_applied) == 2
    assert "counters_lo[STAGES - 1][9]" in result.output_text
    assert "counters_hi[STAGES - 1][9]" in result.output_text


def test_ibex_rvfi_pattern_exact():
    """Exact pattern from the Ibex sv2v failure."""
    text = """module ibex_core;
  localparam int unsigned RVFI_STAGES = 2;
  reg [31:0] rvfi_ext_stage_mhpmcounters [0:RVFI_STAGES - 1][0:9];
  reg [31:0] rvfi_ext_stage_mhpmcountersh [0:RVFI_STAGES - 1][0:9];
  output wire [319:0] rvfi_ext_mhpmcounters;
  output wire [319:0] rvfi_ext_mhpmcountersh;
  assign rvfi_ext_mhpmcounters = rvfi_ext_stage_mhpmcounters[RVFI_STAGES - 1];
  assign rvfi_ext_mhpmcountersh = rvfi_ext_stage_mhpmcountersh[RVFI_STAGES - 1];
  always @(posedge clk) begin
    rvfi_ext_stage_mhpmcounters[i] <= rvfi_ext_stage_mhpmcounters[i - 1];
    rvfi_ext_stage_mhpmcountersh[i] <= rvfi_ext_stage_mhpmcountersh[i - 1];
  end
endmodule
"""
    result = apply_adapter_idempotent(text)
    assert len(result.transforms_applied) == 22
    port_assigns = [t for t in result.transforms_applied if t.name == "expand_2d_array_port_assign"]
    stage_copies = [t for t in result.transforms_applied if t.name == "expand_2d_array_stage_copy"]
    assert len(port_assigns) == 2
    assert len(stage_copies) == 20
    assert "rvfi_ext_stage_mhpmcounters[RVFI_STAGES - 1][9]" in result.output_text
    assert "rvfi_ext_stage_mhpmcounters[RVFI_STAGES - 1][0]" in result.output_text
    assert "rvfi_ext_stage_mhpmcounters[i][0] <= rvfi_ext_stage_mhpmcounters[i - 1][0];" in result.output_text
    assert "rvfi_ext_stage_mhpmcounters[i][9] <= rvfi_ext_stage_mhpmcounters[i - 1][9];" in result.output_text
