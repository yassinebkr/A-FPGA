from types import SimpleNamespace

from formal_toolchain import (
    FormalToolchainCapabilities,
    detect_formal_toolchain,
    detect_tool,
)


def _fake_which(name, path=None):
    # The detector receives the executable name (or a project-local search
    # path), just as shutil.which does.  Returning a stable fake path keeps
    # this test independent of host EDA packages.
    return "/fake/bin/" + name


def test_version_probe_parses_native_formal_tool_output():
    def run(command, **_kwargs):
        output = {
            "yosys": "Yosys 0.50 (git sha1 abc)",
            "sby": "SBY 1.5",
            "z3": "Z3 version 4.13.4 - 64 bit",
        }[command[0].split("/")[-1]]
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    capability = detect_tool("z3", runner=run, which=_fake_which)
    assert capability.available is True
    assert capability.version_tuple == (4, 13, 4)
    assert capability.path == "/fake/bin/z3"


def test_missing_formal_components_are_inconclusive_not_ready():
    def run(command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="Yosys 0.50", stderr="")

    result = detect_formal_toolchain(runner=run, which=lambda *_args, **_kwargs: None)
    assert result.property_ready is False
    assert result.equivalence_ready is False
    assert "yosys" in result.missing("property")
    assert result.as_dict()["property"]["ready"] is False


def test_property_and_equivalence_readiness_require_distinct_tools():
    tools = {}
    for name in ("yosys", "sby", "eqy", "z3", "boolector", "yices"):
        tools[name] = detect_tool(
            name,
            runner=lambda command, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout="tool 1.2.3",
                stderr="",
            ),
            which=lambda executable, **_kwargs: "/fake/bin/" + executable,
        )
    capabilities = FormalToolchainCapabilities(tools)
    assert capabilities.property_ready is True
    assert capabilities.equivalence_ready is True
    assert capabilities.solvers == ("z3", "boolector", "yices")


def test_nonzero_version_probe_is_unavailable_with_reason():
    def run(_command, **_kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="unsupported")

    capability = detect_tool("sby", runner=run, which=_fake_which)
    assert capability.available is False
    assert "exited 2" in capability.error
