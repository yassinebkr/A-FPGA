"""Digest-bound bounded RVFI checks for the pinned Ibex formal profile."""

from __future__ import annotations


SCHEMA = "ibex-rvfi-formal/v1"

_CHECKER = r'''
`ifdef FORMAL
  reg f_past_valid;
  reg f_seen_retire;
  reg [63:0] f_last_order;
  initial begin
    f_past_valid = 1'b0;
    f_seen_retire = 1'b0;
    f_last_order = 64'b0;
  end
  always @(posedge clk_i) begin
    if (!f_past_valid) assume(!rst_ni);
    else assume(rst_ni);
    f_past_valid <= 1'b1;
    assume(boot_addr_i[0] == 1'b0);
    assume(cheriot_enable_i == IbexMuBiOff);
    assume(fetch_enable_i == IbexMuBiOn);
    if (!rst_ni) begin
      f_seen_retire <= 1'b0;
      f_last_order <= 64'b0;
    end else if (rvfi_valid) begin
      assert(rvfi_ixl == 2'd1);
      assert(rvfi_pc_rdata[0] == 1'b0);
      assert((rvfi_mem_rmask == 4'b0) || (rvfi_mem_wmask == 4'b0));
      if (rvfi_rd_addr == 5'b0) assert(rvfi_rd_wdata == 32'b0);
      if (f_seen_retire) assert(rvfi_order == f_last_order + 64'd1);
      f_seen_retire <= 1'b1;
      f_last_order <= rvfi_order;
    end
  end
`endif
'''


def instrument_converted_rtl(text: str) -> str:
    """Insert reviewed properties into the converted top without touching source RTL."""
    marker = "\nendmodule\n"
    offset = text.rfind(marker)
    if offset < 0 or "module ibex_core" not in text:
        raise ValueError("converted artifact does not end with ibex_core")
    if "reg f_seen_retire;" in text:
        raise ValueError("converted artifact is already instrumented")
    return text[:offset] + "\n" + _CHECKER + text[offset:]




__all__ = ["SCHEMA", "instrument_converted_rtl", "run_bounded_rvfi"]
