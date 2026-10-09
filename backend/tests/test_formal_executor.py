from types import SimpleNamespace

from formal_executor import FormalExecutor, FormalProperty, FormalStatus, _connected_harness, property_templates


def test_golden_templates_are_stable_and_named():
    properties = property_templates("counter8")
    assert [item.id for item in properties] == [
        "counter8-reset", "counter8-hold", "counter8-wrap"
    ]


def test_missing_sby_is_inconclusive(tmp_path):
    result = FormalExecutor(sby="missing-sby").execute(
        "module top; endmodule", "top", properties=property_templates("arbiter4"),
        work_dir=tmp_path,
    )
    assert result["status"] == FormalStatus.INCONCLUSIVE
    assert result["ok"] is False
    assert all(item["status"] == "inconclusive" for item in result["results"])


def test_sby_pass_is_proven_with_durable_result(tmp_path, monkeypatch):
    import formal_executor

    def which(binary):
        return "/usr/bin/" + binary

    def runner(command, **kwargs):
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="sby 1.4", stderr="")
        return SimpleNamespace(returncode=0, stdout="engine_0: DONE (PASS, rc=0)", stderr="")

    monkeypatch.setattr(formal_executor.shutil, "which", which)
    result = FormalExecutor(sby="sby", runner=runner).execute(
        "module top(input [3:0] request, output [3:0] grant, output valid); assign grant = request; assign valid = |grant; endmodule",
        "top", properties=property_templates("arbiter4"), work_dir=tmp_path,
    )
    assert result["status"] == "proven"
    assert result["ok"] is True
    assert (tmp_path / "formal-result.json").exists()
    harness = (tmp_path / "formal_harness.sv").read_text()
    assert "top_dut dut" in harness and "bind " not in harness



def test_empty_property_set_is_inconclusive_and_does_not_run_sby(tmp_path, monkeypatch):
    import formal_executor

    def which(binary):
        return "/usr/bin/" + binary

    def runner(command, **kwargs):
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="sby 1.4", stderr="")
        raise AssertionError("empty formal obligation must not invoke sby")

    monkeypatch.setattr(formal_executor.shutil, "which", which)
    result = FormalExecutor(sby="sby", runner=runner).execute(
        "module top; endmodule", "top", properties=[], work_dir=tmp_path,
    )
    assert result["status"] == FormalStatus.INCONCLUSIVE
    assert result["ok"] is False
    assert result["results"] == []


def test_yosys_equivalence_script_isolates_same_named_tops(tmp_path, monkeypatch):
    import formal_executor

    def which(binary):
        return "/usr/bin/yosys" if binary == "yosys" else None

    def runner(command, **kwargs):
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="Yosys 0.40", stderr="")
        return SimpleNamespace(returncode=0, stdout="equivalence successfully proven", stderr="")

    monkeypatch.setattr(formal_executor.shutil, "which", which)
    result = FormalExecutor(yosys="yosys", runner=runner).equivalence(
        "module top(input a, output y); assign y = a; endmodule",
        "module top(input a, output y); assign y = a; endmodule",
        "top", work_dir=tmp_path,
    )
    script = (tmp_path / "equivalence.ys").read_text()
    assert result["status"] == FormalStatus.PROVEN
    assert "read_verilog -formal -sv reference.sv" in script
    assert "prep -top top -flatten" in script
    assert script.count("rename -top gold") == 1
    assert script.count("rename -top gate") == 1
    assert "equiv_make gold gate equiv" in script
    ordering = [
        "rename -top gold", "design -stash gold",
        "read_verilog -formal -sv candidate.sv", "rename -top gate",
        "design -stash gate", "design -reset",
        "design -copy-from gold -as gold gold",
        "design -copy-from gate -as gate gate", "equiv_make gold gate equiv",
    ]
    positions = [script.index(item) for item in ordering]
    assert positions == sorted(positions)



def test_empty_property_set_async_is_inconclusive_without_job_execution(tmp_path):
    import asyncio

    class Job:
        async def run_process(self, *args, **kwargs):
            raise AssertionError("empty formal obligation must not invoke sby")

    result = asyncio.run(FormalExecutor(sby="sby").execute_async(
        "module top; endmodule", "top", properties=[], work_dir=tmp_path, job=Job(),
    ))
    assert result["status"] == FormalStatus.INCONCLUSIVE
    assert result["ok"] is False
    assert result["results"] == []


def test_connected_harness_instantiates_renamed_dut_without_bind():
    rtl = "module top(input a, output y); assign y = a; endmodule"
    renamed, harness = _connected_harness(
        rtl, "top", [FormalProperty("p", "p", "y == a")], None,
    )
    assert "module top_dut" in renamed
    assert "module top;" in harness
    assert "top_dut dut" in harness
    assert ".a(a)" in harness and ".y(y)" in harness
    assert "anyseq" in harness
    assert "bind " not in harness


def test_inout_formal_run_is_inconclusive_even_if_sby_reports_pass(tmp_path, monkeypatch):
    import formal_executor

    monkeypatch.setattr(formal_executor.shutil, "which", lambda binary: "/usr/bin/" + binary)

    def runner(command, **kwargs):
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="tool 1.0", stderr="")
        return SimpleNamespace(returncode=0, stdout="DONE (PASS)", stderr="")

    result = FormalExecutor(sby="sby", runner=runner).execute(
        "module top(inout io); endmodule", "top",
        properties=[FormalProperty("p", "p", "io == io")], work_dir=tmp_path,
    )
    assert result["status"] == FormalStatus.INCONCLUSIVE
    assert "inout" in result["results"][0]["message"]
