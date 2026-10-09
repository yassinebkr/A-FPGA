import pytest

from fpga_profile import (
    FpgaClock,
    FpgaIo,
    FpgaProfile,
    FpgaProgramming,
    FpgaReset,
    FpgaToolchain,
    SCHEMA_VERSION,
    canonical_json,
)


def profile(**changes):
    value = dict(
        profile_id="lab-board",
        profile_version="1.0.0",
        board_id="local-dev-board",
        vendor="example-vendor",
        family="example-family",
        part="example-part",
        toolchain=FpgaToolchain(name="open-source-flow", version="1.0",
                                backend="generic", constraint_format="generic"),
        clock=FpgaClock(port="clk", frequency_mhz=50),
        reset=FpgaReset(port="rst_n"),
        io=[FpgaIo(port="led", direction="output", pin="P1")],
        programming=FpgaProgramming(transport="jtag", programmer="generic-prog"),
    )
    value.update(changes)
    return FpgaProfile(**value)


def test_profile_validates_and_hashes_canonically():
    first = profile().with_hash()
    second = profile().with_hash()
    assert first.schema_version == SCHEMA_VERSION
    assert first.profile_sha256 == second.profile_sha256
    assert len(first.profile_sha256) == 64
    assert first.hash_matches()
    assert canonical_json(first) == canonical_json(second)


def test_profile_hash_detects_content_change():
    hashed = profile().with_hash()
    changed = hashed.model_copy(update={"timeout_s": 45})
    assert not changed.hash_matches()


def test_profile_rejects_duplicate_io_ports_and_pins():
    with pytest.raises(ValueError, match="port mappings must be unique"):
        profile(io=[FpgaIo(port="led", direction="output", pin="P1"),
                    FpgaIo(port="led", direction="input", pin="P2")])
    with pytest.raises(ValueError, match="pin mappings must be unique"):
        profile(io=[FpgaIo(port="led", direction="output", pin="P1"),
                    FpgaIo(port="button", direction="input", pin="P1")])


def test_profile_rejects_invalid_physical_contract():
    with pytest.raises(ValueError):
        FpgaClock(port="clk", frequency_mhz=0)
    with pytest.raises(ValueError):
        profile(clock=FpgaClock(port="clk", frequency_mhz=50),
                reset=FpgaReset(port="clk"))
    with pytest.raises(ValueError):
        profile(programming=FpgaProgramming(transport="usb", programmer=""))
    with pytest.raises(ValueError):
        profile(profile_sha256="not-a-digest")


def test_profile_is_strict_and_does_not_accept_execution_commands():
    with pytest.raises(ValueError):
        profile(shell_command="program --bitstream out.bit")

