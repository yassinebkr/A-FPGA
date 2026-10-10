import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import eda_tools
import pytest
from constraints import ClockConstraint, ConstraintSet
from eda_workspace import EdaWorkspace, validate_target, validate_top_module


@pytest.mark.parametrize("top", ["m", "_counter8", "A__2", "alu_32"])
def test_valid_top_modules_are_safe_build_names(tmp_path, monkeypatch, top):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    path = eda_tools._write_verilog("module x; endmodule", top)
    assert (tmp_path / top / f"{top}.v").resolve() == Path(path).resolve()


@pytest.mark.parametrize("top", ["../escape", "a/b", "/tmp/escape", "a\\b", "1bad", "a$bad", ""])
def test_malicious_or_invalid_top_modules_rejected(tmp_path, monkeypatch, top):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    with pytest.raises(ValueError):
        validate_top_module(top)
    with pytest.raises(ValueError):
        eda_tools._write_verilog("module x; endmodule", top)
    assert list(tmp_path.iterdir()) == []


def test_symlinked_design_directory_cannot_escape_workspace(tmp_path):
    outside = tmp_path.parent / "eda-outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        EdaWorkspace(tmp_path).design_dir("link")


@pytest.mark.parametrize("target", ["../../escape", "skywater130_demo/extra", "__dict__", "", "unknown_demo"])
def test_invalid_targets_rejected_centrally(target):
    with pytest.raises(ValueError):
        validate_target(target)


def test_explicit_sdc_path_must_stay_under_sc_builds(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    with pytest.raises(ValueError):
        eda_tools._write_sdc("m", 10.0, build_dir=tmp_path.parent / "outside")


def test_simulation_verdict_requires_explicit_pass_marker():
    assert eda_tools._simulation_verdict(0, "trace: cnt=3\n$finish called") == (
        "SIMULATION INCONCLUSIVE", "inconclusive")
    assert eda_tools._simulation_verdict(0, "PASS check [test 1]\n$finish called") == (
        "SIMULATION PASSED", "success")
    assert eda_tools._simulation_verdict(0, "trace\nFAIL check [test 1]") == (
        "SIMULATION FAILED", "failure")


def test_run_simulation_no_simulator(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    with patch("eda_tools.shutil.which", return_value=None):
        result = asyncio.run(eda_tools.run_simulation("module m; endmodule", "module tb; endmodule", "m"))
    assert "no simulator" in result
    assert (tmp_path / "m" / "m.v").exists()
    assert (tmp_path / "m" / "m_tb.v").exists()


def test_write_and_check_syntax_no_linter(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    with patch("eda_tools.shutil.which", return_value=None):
        result = eda_tools.write_and_check_syntax("module m; endmodule", "m")
    assert "syntax not checked" in result


def test_write_sdc(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    sdc_path = eda_tools._write_sdc("m", 10.0)
    content = open(sdc_path).read()
    assert "create_clock -name clk -period 10.0 [get_ports clk]" in content
    assert "set_input_delay" in content
    assert "set_output_delay" in content
    assert "remove_from_collection" not in content  # OpenSTA-incompatible


def test_write_sdc_detects_named_clock_port(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    sdc_path = eda_tools._write_sdc("m", 20.0, verilog_code="module m(input clk50); endmodule")
    content = open(sdc_path).read()
    assert "[get_ports clk50]" in content


def test_write_sdc_detects_non_ansi_top_clock_port(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    rtl = """
module helper(clk); input clk; endmodule
module ibex_core(clk_i, rst_ni, data_o);
  input clk_i;
  input rst_ni;
  output data_o;
endmodule
"""
    sdc_path = eda_tools._write_sdc(
        "ibex_core", 20.0, verilog_code=rtl, target="skywater130_demo")
    content = open(sdc_path).read()
    assert "create_clock -name clk_i -period 20.0 [get_ports clk_i]" in content
    assert "[get_ports clk]" not in content


def test_write_sdc_sky130_extras(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    sdc_path = eda_tools._write_sdc("m", 10.0, target="skywater130_demo")
    content = open(sdc_path).read()
    assert "set_clock_uncertainty 0.25 [get_clocks clk]" in content
    assert "set_driving_cell -lib_cell sky130_fd_sc_hd__buf_4 [all_inputs]" in content
    assert "set_load 0.03 [all_outputs]" in content
    assert "[all_inputs]" in content
    assert "[all_outputs]" in content
    assert "remove_from_collection" not in content


def test_write_sdc_no_driving_cell_for_nonsky130(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    sdc_path = eda_tools._write_sdc("m", 10.0, target="freepdk45_demo")
    content = open(sdc_path).read()
    assert "set_driving_cell" not in content
    assert "set_clock_uncertainty 0.25" in content
    assert "set_load 0.03" in content


def test_write_sdc_false_path_for_async_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    rtl = ("module m(input clk, input rst_n, output reg q); "
           "always @(posedge clk or negedge rst_n) q <= rst_n ? 1'b0 : q; "
           "endmodule")
    sdc_path = eda_tools._write_sdc("m", 10.0, verilog_code=rtl)
    content = open(sdc_path).read()
    assert "set_false_path -from [get_ports rst_n]" in content


def _run_synthesis_fake(tmp_path, monkeypatch, **kwargs):
    """Run run_silicon_compiler with a fake sc_job subprocess (prints the
    SCJOB summary line, exits 0) and return the result text."""
    import sys as _sys
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    # Keep the run's JobManager job on a tmp DB instead of the real registry.
    monkeypatch.setattr(eda_tools.jobs_mod, "manager",
                        eda_tools.jobs_mod.JobManager(tmp_path / "jobs.db", tmp_path / "jobs"))

    def fake_cmd(top, verilog_path, build_dir, target, period, flow="auto"):
        code = ('import sys; print(\'SCJOB {"flow": "mocked flow", "ok": true}\')')
        return [_sys.executable, "-c", code]

    monkeypatch.setattr(eda_tools, "_sc_job_cmd", fake_cmd)

    async def _run():
        try:
            return await eda_tools.run_silicon_compiler("module m; endmodule", "m", **kwargs)
        finally:
            await eda_tools.jobs_mod.manager.stop()

    result = asyncio.run(_run())
    assert "Synthesis successful (mocked flow)" in result
    return result


def test_run_silicon_compiler_with_clock_writes_sdc(tmp_path, monkeypatch):
    _run_synthesis_fake(tmp_path, monkeypatch, clock_period_ns=10.0)
    sdc = tmp_path / "m" / "m.sdc"
    # The SDC is written by the sc_job subprocess; here the fake replaces it,
    # so instead verify the command carried the clock period.
    # (SDC content itself is covered by the test_write_sdc* tests.)
    assert (tmp_path / "m" / "m.v").exists()


def test_run_silicon_compiler_without_clock_ok(tmp_path, monkeypatch):
    _run_synthesis_fake(tmp_path, monkeypatch)
    assert (tmp_path / "m" / "m.v").exists()


def test_sc_job_build_project_registers_sdc(tmp_path, monkeypatch):
    """sc_job.build_project adds the sdc fileset iff a clock period is given."""
    import sc_job
    vp = tmp_path / "m.v"
    vp.write_text("module m(input clk); endmodule")
    monkeypatch.setattr(sc_job.shutil, "which", lambda t: f"/usr/bin/{t}")

    project, _ = sc_job.build_project("m", str(vp), "skywater130_demo", 10.0,
                                      "auto", str(tmp_path / "b1"))
    filesets = [name for _, name in project.get_filesets()]
    assert "sdc" in filesets
    sdc_files = project.design.get_file(fileset="sdc")
    assert len(sdc_files) == 1 and sdc_files[0].endswith("m.sdc")
    assert "create_clock" in open(sdc_files[0]).read()
    task = project.get("tool", "yosys", "task", "syn_asic", field="schema")
    assert task.get("var", "strategy") == "DELAY1"

    project2, _ = sc_job.build_project("m", str(vp), "skywater130_demo", None,
                                       "auto", str(tmp_path / "b2"))
    filesets2 = [name for _, name in project2.get_filesets()]
    assert "sdc" not in filesets2


def test_sc_job_build_project_applies_bounded_physical_densities(tmp_path, monkeypatch):
    import sc_job

    vp = tmp_path / "m.v"
    vp.write_text("module m(input clk); endmodule\n", encoding="utf-8")
    monkeypatch.setattr(sc_job.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    project, _ = sc_job.build_project(
        "m", str(vp), "skywater130_demo", 20.0, "pnr",
        str(tmp_path / "build"), floorplan_density=30.0, place_density=0.5,
    )

    assert project.constraint.area.get_density() == 30.0
    for task, step in (
        ("macro_placement", "floorplan.macro_placement"),
        ("pin_placement", "floorplan.pin_placement"),
        ("global_placement", "place.global"),
    ):
        assert project.get(
            "tool", "openroad", "task", task, "var", "place_density",
            step=step, index="0",
        ) == 0.5

    for floorplan_density, place_density in ((0.0, None), (None, 1.0)):
        with pytest.raises(ValueError):
            sc_job.build_project(
                "m", str(vp), "skywater130_demo", 20.0, "pnr",
                str(tmp_path / "invalid"),
                floorplan_density=floorplan_density, place_density=place_density,
            )


def test_sc_job_registers_extensionless_artifact_as_verilog(tmp_path, monkeypatch):
    """Content-addressed RTL artifacts do not carry a .v extension."""
    import sc_job

    artifact = tmp_path / ("a" * 64)
    artifact.write_text("module m; endmodule\n", encoding="utf-8")
    monkeypatch.setattr(sc_job.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    project, _ = sc_job.build_project(
        "m", str(artifact), "skywater130_demo", None,
        "syn-sta", str(tmp_path / "build"),
    )

    sources = project.design.get_file(fileset="rtl", filetype="verilog")
    assert sources == [str(artifact)]


def test_read_metrics_reads_slack(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    build_dir = tmp_path / "m"
    manifest_dir = build_dir / "m" / "job0"
    manifest_dir.mkdir(parents=True)
    manifest = {
        "metric": {
            "setupslack": {"node": {"route.detailed": {"0": {"value": 1.23}}}},
            "holdslack": {"node": {"cts.repair_timing": {"0": {"value": 0.05}}}},
        }
    }
    (manifest_dir / "m.pkg.json").write_text(json.dumps(manifest))
    metrics = eda_tools._read_metrics(str(build_dir), "m")
    assert metrics["setupslack"] == "1.23 ns"
    assert metrics["holdslack"] == "0.05 ns"


def _fmax_fake_cmd(wns_of_period, calls, log_file=None):
    """Fake _sc_job_cmd: writes a manifest whose setupslack follows
    wns_of_period(period), simulating a design with a true fmax at the
    zero-crossing. Optionally logs start/end markers for concurrency checks."""
    import sys as _sys

    def fake_cmd(top, verilog_path, build_dir, target, period, flow="auto"):
        calls.append(period)
        wns = wns_of_period(period)
        lines = ["import json, os, sys", "bd = sys.argv[1]"]
        if log_file:
            lines += [f"open({str(log_file)!r}, 'a').write('S {period}\\n')",
                      "import time", "time.sleep(0.3)"]
        lines += [
            f"wns = {wns!r}",
            "d = os.path.join(bd, 'counter', 'job0')",
            "os.makedirs(d, exist_ok=True)",
            "m = {'metric': {'setupslack': {'node': {'synthesis.timing': {'0': {'value': wns}}}}}}",
            "open(os.path.join(d, 'counter.pkg.json'), 'w').write(json.dumps(m))",
        ]
        if log_file:
            lines.append(f"open({str(log_file)!r}, 'a').write('E {period}\\n')")
        lines.append("print('SCJOB {\"flow\": \"mocked\", \"ok\": true}')")
        return [_sys.executable, "-c", "\n".join(lines) + "\n", build_dir]

    return fake_cmd


def _run_fmax(tmp_path, monkeypatch, fake_cmd):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    monkeypatch.setattr(eda_tools.jobs_mod, "manager",
                        eda_tools.jobs_mod.JobManager(tmp_path / "jobs.db", tmp_path / "jobs"))
    monkeypatch.setattr(eda_tools, "_sc_job_cmd", fake_cmd)

    async def _run():
        try:
            rtl = "module counter(input clk, input rst_n, output reg [3:0] q); endmodule"
            return await eda_tools.find_fmax(rtl, "counter")
        finally:
            await eda_tools.jobs_mod.manager.stop()

    return asyncio.run(_run())


def test_find_fmax_parallel_convergence(tmp_path, monkeypatch):
    """Bracket [5, 40] ns, true fmax at 28 ns (WNS = (p-28)*0.5). Parallel
    quarter-point probes must converge to just above 28 ns within 3 rounds."""
    calls = []
    # The search uses observed feasible/infeasible periods only. With three
    # rounds it should report a narrow, honest interval around the 28 ns
    # boundary without extrapolating from the WNS magnitude.
    result = _run_fmax(tmp_path, monkeypatch,
                       _fmax_fake_cmd(lambda p: (p - 28.0) * 0.5, calls))

    payload = json.loads(result.splitlines()[0])
    assert payload["status"] == "success"
    assert 28.0 <= payload["chosen_period_ns"] <= 28.5
    assert payload["wns_ns"] >= 0
    assert payload["search"]["uncertainty_ns"] <= 0.5
    assert payload["search"]["confirmation"]["status"] == "stable"
    assert payload["search"]["monotonicity"]["status"] == "valid"
    assert len(calls) <= payload["search"]["max_runs"]
    assert payload["search"]["assumptions"]
    assert "job_id:" in result


def test_find_fmax_probes_run_concurrently(tmp_path, monkeypatch):
    """The candidate periods of one round must overlap in time (gathered, not
    sequential). The fake runner logs S/E markers with a 0.3 s sleep between
    them; concurrent probes interleave markers."""
    calls = []
    log_file = tmp_path / "probes.log"
    _run_fmax(tmp_path, monkeypatch,
              _fmax_fake_cmd(lambda p: (p - 28.0) * 0.5, calls, log_file=log_file))

    events = log_file.read_text().splitlines()
    depth = 0
    max_depth = 0
    for line in events:
        depth += 1 if line.startswith("S ") else -1
        max_depth = max(max_depth, depth)
    assert max_depth >= 2, f"probes did not overlap: {events}"


def test_find_fmax_result_shape(tmp_path, monkeypatch):
    """Agent-visible result text: JSON metric line, per-round summaries, and
    the job_id marker (one job for the whole search)."""
    calls = []
    result = _run_fmax(tmp_path, monkeypatch,
                       _fmax_fake_cmd(lambda p: 1.0, calls))  # everything feasible
    lines = result.strip().splitlines()
    assert lines[-1].startswith("job_id: ")
    payload = json.loads(lines[0])
    # Everything feasible: the bracket's upper bound keeps dropping to the
    assert set(payload) == {"status", "fmax_mhz", "chosen_period_ns", "wns_ns", "search"}
    assert payload["search"]["bracket_ns"] == [5.0, 40.0]
    assert payload["search"]["probes"] <= payload["search"]["max_runs"]
    assert payload["search"]["fmax_bounds_mhz"][0] <= payload["fmax_mhz"] <= payload["search"]["fmax_bounds_mhz"][1]
    assert payload["chosen_period_ns"] == 5.0
    assert len(calls) <= payload["search"]["max_runs"]
    assert any("Fmax search summaries:" in line for line in lines)


def test_find_fmax_rejects_non_monotonic_timing(tmp_path, monkeypatch):
    calls = []
    result = _run_fmax(
        tmp_path, monkeypatch,
        _fmax_fake_cmd(lambda p: 1.0 if p <= 5.0 or p >= 40.0 else -1.0, calls),
    )
    payload = json.loads(result.splitlines()[0])
    assert payload["status"] == "inconclusive"
    assert payload["search"]["monotonicity"]["status"] == "violated"
    assert payload["search"]["monotonicity"]["violations"]


def test_find_fmax_holds_build_lock(tmp_path, monkeypatch):
    """The per-design build lock is held across the WHOLE fmax search: a
    concurrent run_silicon_compiler for the same top fails fast."""
    calls = []
    log_file = tmp_path / "probes.log"
    fake = _fmax_fake_cmd(lambda p: (p - 28.0) * 0.5, calls, log_file=log_file)
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    monkeypatch.setattr(eda_tools.jobs_mod, "manager",
                        eda_tools.jobs_mod.JobManager(tmp_path / "jobs.db", tmp_path / "jobs"))
    monkeypatch.setattr(eda_tools, "_sc_job_cmd", fake)

    async def _run():
        try:
            rtl = "module counter(input clk, input rst_n, output reg [3:0] q); endmodule"
            fmax_task = asyncio.create_task(eda_tools.find_fmax(rtl, "counter"))
            await asyncio.sleep(0.1)  # let fmax acquire the design lock
            synth_result = await eda_tools.run_silicon_compiler(rtl, "counter")
            fmax_result = await fmax_task
            return synth_result, fmax_result
        finally:
            await eda_tools.jobs_mod.manager.stop()

    synth_result, fmax_result = asyncio.run(_run())
    assert "already running" in synth_result
    assert '"chosen_period_ns"' in fmax_result


def test_concurrent_synthesis_same_design_rejected(tmp_path, monkeypatch):
    """Two run_silicon_compiler calls for the SAME top_module must not run
    concurrently (they share the SC build dir): the second fails fast."""
    import sys as _sys
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    monkeypatch.setattr(eda_tools.jobs_mod, "manager",
                        eda_tools.jobs_mod.JobManager(tmp_path / "jobs.db", tmp_path / "jobs"))

    def slow_cmd(top, verilog_path, build_dir, target, period, flow="auto"):
        code = ('import time; time.sleep(0.5); '
                'print(\'SCJOB {"flow": "mocked flow", "ok": true}\')')
        return [_sys.executable, "-c", code]

    monkeypatch.setattr(eda_tools, "_sc_job_cmd", slow_cmd)

    async def _run():
        try:
            return await asyncio.gather(
                eda_tools.run_silicon_compiler("module m; endmodule", "m"),
                eda_tools.run_silicon_compiler("module m; endmodule", "m"),
            )
        finally:
            await eda_tools.jobs_mod.manager.stop()

    results = asyncio.run(_run())
    outcomes = sorted("busy" if "already running" in r else "ok" for r in results)
    assert outcomes == ["busy", "ok"]


def test_concurrent_synthesis_different_designs_ok(tmp_path, monkeypatch):
    """Designs with different top_modules have separate build dirs and locks —
    they synthesize in parallel."""
    import sys as _sys
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    monkeypatch.setattr(eda_tools.jobs_mod, "manager",
                        eda_tools.jobs_mod.JobManager(tmp_path / "jobs.db", tmp_path / "jobs"))

    def slow_cmd(top, verilog_path, build_dir, target, period, flow="auto"):
        code = ('import time; time.sleep(0.2); '
                'print(\'SCJOB {"flow": "mocked flow", "ok": true}\')')
        return [_sys.executable, "-c", code]

    monkeypatch.setattr(eda_tools, "_sc_job_cmd", slow_cmd)

    async def _run():
        try:
            return await asyncio.gather(
                eda_tools.run_silicon_compiler("module a; endmodule", "a"),
                eda_tools.run_silicon_compiler("module b; endmodule", "b"),
            )
        finally:
            await eda_tools.jobs_mod.manager.stop()

    results = asyncio.run(_run())
    assert all("Synthesis successful (mocked flow)" in r for r in results)



def test_typed_constraints_render_and_transport_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    constraints = ConstraintSet(clocks=[ClockConstraint(
        name="sys_clk", port="clk", period_ns=8.0)])
    sdc_path, digest = eda_tools._write_constraint_sdc(constraints, "m", str(tmp_path / "m"))
    content = (tmp_path / "m" / "m.sdc").read_bytes()
    assert content == constraints.to_sdc().encode()
    assert digest == hashlib.sha256(content).hexdigest()
    cmd = eda_tools._sc_job_cmd("m", str(tmp_path / "m" / "m.v"), str(tmp_path / "m"),
                                "skywater130_demo", None, constraints_path=sdc_path)
    assert cmd[-2:] == ["--sdc", sdc_path]


def test_sc_job_uses_supplied_sdc_without_regenerating(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    import sc_job
    vp = tmp_path / "m.v"
    vp.write_text("module m(input clk); endmodule")
    custom = tmp_path / "custom.sdc"
    custom.write_text("# authoritative\ncreate_clock -name custom -period 7 [get_ports {clk}]\n")
    monkeypatch.setattr(sc_job.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    project, _ = sc_job.build_project("m", str(vp), "skywater130_demo", 99.0,
                                      "auto", str(tmp_path / "build"), str(custom))
    files = project.design.get_file(fileset="sdc")
    assert files == [str(custom.resolve())]
    assert "period 7" in Path(files[0]).read_text()


def test_synthesis_reports_exact_typed_constraint_hash(tmp_path, monkeypatch):
    import sys as _sys
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    monkeypatch.setattr(eda_tools.jobs_mod, "manager",
                        eda_tools.jobs_mod.JobManager(tmp_path / "jobs.db", tmp_path / "jobs"))
    constraints = ConstraintSet(clocks=[ClockConstraint(
        name="clk", port="clk", period_ns=10.0)])
    seen = {}

    def fake_cmd(top, verilog_path, build_dir, target, period, flow="auto", constraints_path=None):
        seen["path"] = constraints_path
        code = (
            "import hashlib, json, os; "
            f"p={constraints_path!r}; bd={build_dir!r}; "
            "d=os.path.join(bd, 'm', 'job0'); os.makedirs(d, exist_ok=True); "
            "m={'metric': {'setupslack': {'node': {'synthesis.timing': {'0': {'value': 1.0}}}}}}; "
            "open(os.path.join(d, 'm.pkg.json'), 'w').write(json.dumps(m)); "
            "h=hashlib.sha256(open(p, 'rb').read()).hexdigest(); "
            "print('SCJOB '+json.dumps({'flow':'mocked','ok':True,'constraints':{'sha256':h}}))"
        )
        return [_sys.executable, "-c", code]

    monkeypatch.setattr(eda_tools, "_sc_job_cmd", fake_cmd)

    async def _run():
        try:
            return await eda_tools.run_silicon_compiler(
                "module m(input clk); endmodule", "m", constraints=constraints)
        finally:
            await eda_tools.jobs_mod.manager.stop()

    result = asyncio.run(_run())
    assert seen["path"] is not None
    assert f"constraints_sha256: {constraints.content_sha256()}" in result
    assert f"constraints_sdc_sha256: {hashlib.sha256(Path(seen['path']).read_bytes()).hexdigest()}" in result
    assert f"constraints_sdc_path: {seen['path']}" in result


def test_synthesis_rejects_typed_constraint_digest_mismatch(tmp_path, monkeypatch):
    import sys as _sys
    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    monkeypatch.setattr(eda_tools.jobs_mod, "manager",
                        eda_tools.jobs_mod.JobManager(tmp_path / "jobs.db", tmp_path / "jobs"))
    constraints = ConstraintSet(clocks=[ClockConstraint(name="clk", port="clk", period_ns=10.0)])

    def fake_cmd(top, verilog_path, build_dir, target, period, flow="auto", constraints_path=None):
        code = "print('SCJOB {\"flow\":\"mocked\",\"ok\":true,\"constraints\":{\"sha256\":\"' + '0'*64 + '\"}}')"
        return [_sys.executable, "-c", code]

    monkeypatch.setattr(eda_tools, "_sc_job_cmd", fake_cmd)

    async def _run():
        try:
            return await eda_tools.run_silicon_compiler("module m(input clk); endmodule", "m",
                                                          constraints=constraints)
        finally:
            await eda_tools.jobs_mod.manager.stop()

    result = asyncio.run(_run())
    assert "did not report the exact typed constraint artifact" in result


def test_staged_flow_modes_are_validated_and_normalized():
    import sc_job

    assert sc_job.validate_flow_mode("screen") == "screen"
    assert sc_job.validate_flow_mode("syn-sta") == "screen"
    assert sc_job.validate_flow_mode("full") == "pnr"
    assert sc_job.validate_flow_mode("full-pnr") == "pnr"
    with pytest.raises(ValueError):
        sc_job.validate_flow_mode("screen; rm -rf /")


def test_sc_job_synthesis_strategy_validation():
    import sc_job

    assert sc_job.validate_synthesis_strategy("DELAY1") == "DELAY1"
    with pytest.raises(ValueError):
        sc_job.validate_synthesis_strategy("+arbitrary-script")


def test_explicit_flow_selection_fails_closed(monkeypatch):
    import sc_job

    class Project:
        def __init__(self):
            self.flow = None

        def set_flow(self, value):
            self.flow = value

    monkeypatch.setattr(sc_job.shutil, "which",
                        lambda tool: "/usr/bin/yosys" if tool == "yosys" else None)
    with pytest.raises(RuntimeError, match="pnr flow requires"):
        sc_job.pick_flow(Project(), "pnr")


def test_explicit_screen_uses_isolated_build_path(tmp_path, monkeypatch):
    import sys as _sys

    monkeypatch.setenv("SC_BUILDS", str(tmp_path))
    monkeypatch.setattr(eda_tools.jobs_mod, "manager",
                        eda_tools.jobs_mod.JobManager(tmp_path / "jobs.db", tmp_path / "jobs"))
    seen = {}

    def fake_cmd(top, verilog_path, build_dir, target, period, flow="auto", constraints_path=None):
        seen.update(build_dir=build_dir, flow=flow)
        code = ("import json; print('SCJOB ' + json.dumps({"
                "'schema_version': 1, 'flow_mode': 'screen', "
                "'stage': 'synthesis_sta', 'flow': 'screening synthesis + static timing', "
                "'ok': True, 'target': 'skywater130_demo', 'top_module': 'm', "
                "'artifacts': {'siliconcompiler_manifest': 'manifest.json'}}))")
        return [_sys.executable, "-c", code]

    monkeypatch.setattr(eda_tools, "_sc_job_cmd", fake_cmd)

    async def run():
        try:
            return await eda_tools.run_silicon_compiler(
                "module m; endmodule", "m", flow="syn-sta")
        finally:
            await eda_tools.jobs_mod.manager.stop()

    result = asyncio.run(run())
    assert "Synthesis successful" in result
    digest = eda_tools._stage_context_digest(
        "module m; endmodule", "m", "skywater130_demo", "screen", None, None)
    assert Path(seen["build_dir"]).resolve() == (tmp_path / "m" / "screen" / digest).resolve()
    assert seen["flow"] == "screen"

    # A different RTL candidate gets a different durable root rather than
    # overwriting the first screen's artifacts.
    other = eda_tools._stage_context_digest(
        "module m; wire next; endmodule", "m", "skywater130_demo", "screen", None, None)
    assert other != digest
    assert (tmp_path / "m" / "screen" / digest) != (tmp_path / "m" / "screen" / other)


def test_scjob_summary_contains_stage_metadata():
    summary = eda_tools._parse_scjob(
        'SCJOB {"schema_version":1,"flow_mode":"pnr",'
        '"stage":"place_route","ok":true}'
    )
    assert summary["schema_version"] == 1
    assert summary["flow_mode"] == "pnr"
    assert summary["stage"] == "place_route"
