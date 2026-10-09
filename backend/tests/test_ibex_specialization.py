import pytest

from hardware_project import HardwareProjectUpdate, SourceInput
from ibex_specialization import IbexSpecializationError, specialize_ibex_update


TOP = """module ibex_core #(
  parameter bit RV32E = 1'b0,
  parameter rv32m_e RV32M = RV32MFast,
  parameter rv32b_e RV32B = RV32BNone,
  parameter rv32zc_e RV32ZC = RV32ZcaZcbZcmp,
  parameter bit DbgTriggerEn = 1'b0,
  parameter bit SecureIbex = 1'b0,
  parameter bit WritebackStage = 1'b0,
  parameter bit BranchTargetALU = 1'b0
) (input logic clk_i); endmodule
"""


def _update(content=TOP):
    return HardwareProjectUpdate(
        top_module="ibex_core",
        files=[SourceInput(path="rtl/ibex_core.sv", content=content)],
        build_targets={"rv32i-known-core-v1": {"fixture": "ibex-minimal"}},
    )


def test_specialization_is_explicit_and_digest_visible():
    result, report = specialize_ibex_update(_update(), {
        "RV32E": 0, "RV32M": "none", "RV32B": "none", "RV32ZC": "zca",
        "DbgTriggerEn": 0, "SecureIbex": 0,
        "WritebackStage": 1, "BranchTargetALU": 0,
    })
    body = result.files[0].content
    assert "RV32M = RV32MNone" in body
    assert "RV32ZC = RV32Zca" in body
    assert "WritebackStage = 1'b1" in body
    assert "BranchTargetALU = 1'b0" in body
    assert report["input_sha256"] != report["output_sha256"]
    assert len(report["transforms"]) == 8
    assert result.build_targets["rv32i-known-core-v1"]["parameter_specialization"] == report


def test_specialization_fails_closed_on_source_drift_or_unsupported_value():
    with pytest.raises(IbexSpecializationError, match="declaration for RV32M"):
        specialize_ibex_update(_update(TOP.replace("RV32M =", "M_MODE =")), {
            "RV32M": "none",
        })
    with pytest.raises(IbexSpecializationError, match="unsupported value"):
        specialize_ibex_update(_update(), {"RV32M": "fast"})
