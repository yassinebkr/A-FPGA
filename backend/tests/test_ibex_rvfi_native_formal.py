from ibex_rvfi_native_formal import DEFAULT_IMAGE, SCHEMA


def test_native_formal_configuration_is_pinned_and_bounded():
    assert SCHEMA == "ibex-rvfi-native-formal/v1"
    assert "@sha256:" in DEFAULT_IMAGE
    assert DEFAULT_IMAGE.endswith("f91b21d75f79871f9ccf37451020b5d2f7b3236881a997ff709c561e8280a30f")
