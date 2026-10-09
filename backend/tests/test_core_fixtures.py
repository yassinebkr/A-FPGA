import pytest

from core_fixtures import (
    apply_execution_profile,
    CoreFixtureError,
    IBEX_MINIMAL,
    PICORV32,
    fixture_for,
    list_fixtures,
    qualification_ready,
    validate_fixture,
    validate_import,
)
from hardware_project import HardwareProjectUpdate, SourceInput


def picorv_files():
    return {
        "picorv32.v": "module picorv32(input wire clk, output wire trap); assign trap = 1'b0; endmodule\n"
    }


def test_catalog_is_source_free_and_has_two_qualification_ladders():
    catalog = list_fixtures()
    assert {item["key"] for item in catalog} == {"picorv32-control", "ibex-minimal"}
    assert all("content" not in item for item in catalog)
    pico = fixture_for("picorv32-control")
    assert pico.source.commit == "ef203c2b0a3fb793280f5114941416c425c5b461"
    assert pico.license_spdx == "ISC"
    assert qualification_ready("picorv32-control")
    assert qualification_ready("ibex-minimal")


def test_ibex_pin_and_metadata_are_ready_for_controlled_import():
    validate_fixture(IBEX_MINIMAL)
    assert IBEX_MINIMAL.source.commit == "654ac71f91f7da5dc71942c9ba8d3103666c484e"
    with pytest.raises(CoreFixtureError, match="missing compile-order"):
        validate_import(IBEX_MINIMAL, {"rtl/ibex_top.sv": "module ibex_top; endmodule\n"})


def test_import_requires_declared_sources_and_preserves_compile_order():
    update = validate_import(PICORV32, picorv_files())
    assert update.top_module == "picorv32"
    assert update.compile_order == ["picorv32.v"]
    assert update.files[0].content.startswith("module picorv32")
    assert update.build_targets["rv32i-known-core-v1"]["source_commit"] == PICORV32.source.commit

    with pytest.raises(CoreFixtureError, match="missing compile-order"):
        validate_import(PICORV32, {"other.v": "module other; endmodule\n"})


def test_import_rejects_paths_outside_fixture_roots_and_unsafe_names():
    with pytest.raises(CoreFixtureError, match="outside declared source roots"):
        validate_import(PICORV32, {**picorv_files(), "docs/readme.md": "not RTL"})
    with pytest.raises(CoreFixtureError, match="unsafe imported path"):
        validate_import(PICORV32, {"../picorv32.v": "module picorv32; endmodule\n"})


def test_ibex_execution_profiles_separate_rvfi_from_physical_synthesis():
    update = HardwareProjectUpdate(
        top_module="ibex_core",
        files=[SourceInput(path="rtl/ibex_core.sv", content="""module ibex_core #(
  parameter bit RV32E = 1'b0,
  parameter rv32m_e RV32M = RV32MFast,
  parameter rv32b_e RV32B = RV32BNone,
  parameter rv32zc_e RV32ZC = RV32ZcaZcbZcmp,
  parameter bit DbgTriggerEn = 1'b0,
  parameter bit SecureIbex = 1'b0
); endmodule
""")],
        defines={"SYNTHESIS": "1", "RVFI": "1", "KEEP": None},
        build_targets={IBEX_MINIMAL.qualification_profile: {"fixture": IBEX_MINIMAL.key}},
        provenance={"commit": IBEX_MINIMAL.source.commit},
    )
    synthesis = apply_execution_profile(IBEX_MINIMAL, update, "synthesis")
    assert synthesis.defines == {"SYNTHESIS": "1", "KEEP": None}
    target = synthesis.build_targets[IBEX_MINIMAL.qualification_profile]
    assert target["execution_profile"] == "synthesis"
    assert target["removed_defines"] == ["RVFI"]
    assert synthesis.provenance["execution_profile"] == "synthesis"

    formal = apply_execution_profile(IBEX_MINIMAL, synthesis, "formal")
    assert formal.defines["RVFI"] == "1"
    assert formal.build_targets[IBEX_MINIMAL.qualification_profile]["removed_defines"] == []

    with pytest.raises(CoreFixtureError, match="unknown execution profile"):
        apply_execution_profile(IBEX_MINIMAL, update, "timing-ish")
