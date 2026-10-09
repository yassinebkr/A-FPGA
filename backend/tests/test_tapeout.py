import asyncio
from pathlib import Path

from jobs import JobManager
from tapeout import (
    TinyTapeoutConfig,
    detect_tiny_tapeout_capabilities,
    inspect_klayout_drc,
    run_tiny_tapeout_hardening,
    run_tiny_tapeout_hardening_job,
)


WRAPPER = """
`default_nettype none
module tt_um_demo (
    input wire [7:0] ui_in, output wire [7:0] uo_out,
    input wire [7:0] uio_in, output wire [7:0] uio_out,
    output wire [7:0] uio_oe, input wire ena, input wire clk, input wire rst_n
);
assign uo_out = ui_in;
assign uio_out = 8'b0;
assign uio_oe = 8'b0;
endmodule
"""


def _template(tmp_path: Path, *, tool: bool = False) -> Path:
    root = tmp_path / "template"
    (root / "src").mkdir(parents=True)
    (root / "test").mkdir()
    (root / "info.yaml").write_text(
        "project:\n  title: \"Example\"\n  author: \"A\"\n"
        "  description: \"D\"\n  clock_hz: 50000000\n"
        "  top_module: \"tt_um_example\"\n  source_files:\n    - \"project.v\"\n",
        encoding="utf-8")
    (root / "src" / "config.json").write_text(
        '{"PNR_SDC_FILE": "dir::pnr.sdc", '
        '"SIGNOFF_SDC_FILE": "dir::signoff.sdc"}\n', encoding="utf-8")
    (root / "src" / "pnr.sdc").write_text(
        "create_clock -name clk -period 20.0 [get_ports clk]\n", encoding="utf-8")
    (root / "src" / "signoff.sdc").write_text(
        "create_clock -name clk -period 20.0 [get_ports clk]\n", encoding="utf-8")
    (root / "test" / "Makefile").write_text("PROJECT_SOURCES = project.v\n", encoding="utf-8")
    (root / "test" / "tb.v").write_text("tt_um_example user_project ( );\n", encoding="utf-8")
    if tool:
        tool_path = root / "tt"
        tool_path.mkdir()
        (tool_path / "tt_tool.py").write_text(
            "import sys\nprint('hardening ok')\nsys.exit(0)\n", encoding="utf-8")
    return root


def test_missing_hardening_prerequisites_are_inconclusive(tmp_path, monkeypatch):
    template = _template(tmp_path)
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr("tapeout.shutil.which", lambda _: None)
    config = TinyTapeoutConfig(template_dir=template, build_root=tmp_path / "build")
    caps = detect_tiny_tapeout_capabilities(config)
    assert caps.ready is False
    result = run_tiny_tapeout_hardening(WRAPPER, "demo", config=config)
    assert result["status"] == "inconclusive"
    assert result["ok"] is False
    assert result["manufacturing_candidate"] is False


def test_hardening_prepares_isolated_inputs_and_runs_real_command(tmp_path, monkeypatch):
    template = _template(tmp_path, tool=True)
    pdk = tmp_path / "pdk"
    pdk.mkdir()
    monkeypatch.setenv("PDK_ROOT", str(pdk))
    monkeypatch.setenv("PDK", "sky130A")
    monkeypatch.setattr("tapeout.shutil.which", lambda _: "/usr/bin/docker")
    config = TinyTapeoutConfig(template_dir=template, build_root=tmp_path / "build")
    result = run_tiny_tapeout_hardening(WRAPPER, "demo", config=config)
    assert result["status"] == "passed"
    assert result["ok"] is True
    assert result["signoff"] is False
    workspace = Path(result["workspace"])
    assert (workspace / "src" / "tt_um_demo.v").read_text() == WRAPPER
    assert 'top_module: "tt_um_demo"' in (workspace / "info.yaml").read_text()
    assert "PROJECT_SOURCES = tt_um_demo.v" in (workspace / "test" / "Makefile").read_text()
    assert "tt_um_demo user_project" in (workspace / "test" / "tb.v").read_text()
    assert (workspace / "hardening.log").is_file()
    assert (workspace / "src" / "pnr.sdc").is_file()
    assert (workspace / "src" / "signoff.sdc").is_file()
    assert result["inputs"]["sdc_files"]["PNR_SDC_FILE"]["path"] == "src/pnr.sdc"


def test_wrapper_module_and_workspace_are_validated(tmp_path):
    config = TinyTapeoutConfig(template_dir=_template(tmp_path), build_root=tmp_path / "build")
    result = run_tiny_tapeout_hardening("module bad; endmodule", "../escape", config=config)
    assert result["status"] == "inconclusive"
    assert "input" in result["reason"]


def test_klayout_drc_disabled_is_explicitly_not_run(tmp_path):
    workspace = tmp_path / "workspace" / "src"
    workspace.mkdir(parents=True)
    (workspace / "config_merged.json").write_text(
        '{"RUN_KLAYOUT_DRC": 0}\n', encoding="utf-8")
    result = inspect_klayout_drc(workspace.parent)
    assert result["status"] == "not_run"
    assert result["enabled"] is False
    assert "disabled" in result["reason"]


def test_klayout_drc_enabled_without_metric_is_inconclusive(tmp_path):
    workspace = tmp_path / "workspace" / "src"
    workspace.mkdir(parents=True)
    (workspace / "config_merged.json").write_text(
        '{"RUN_KLAYOUT_DRC": 1}\n', encoding="utf-8")
    result = inspect_klayout_drc(workspace.parent)
    assert result["status"] == "inconclusive"
    assert "was not reported" in result["reason"]


def test_klayout_drc_metric_zero_passes_and_nonzero_fails(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "config_merged.json").write_text(
        '{"RUN_KLAYOUT_DRC": true}\n', encoding="utf-8")
    state = workspace / "runs" / "wokwi" / "72-misc"
    state.mkdir(parents=True)
    state_file = state / "state_out.json"
    state_file.write_text(
        '{"metrics": {"klayout__drc_error__count": 0}}\n', encoding="utf-8")
    result = inspect_klayout_drc(workspace)
    assert result["status"] == "passed"
    assert result["metric"]["value"] == 0

    state_file.write_text(
        '{"metrics": {"klayout__drc_error__count": 3}}\n', encoding="utf-8")
    result = inspect_klayout_drc(workspace)
    assert result["status"] == "failed"
    assert "3 errors" in result["reason"]



async def _wait_for_job_process(manager):
    for _ in range(200):
        jobs = list(manager.jobs.values())
        if jobs and jobs[0].proc is not None:
            return jobs[0]
        await asyncio.sleep(0.01)
    raise AssertionError("Tiny Tapeout process did not start")


def test_job_streams_output_and_cancellation_kills_process_group(tmp_path, monkeypatch):
    template = _template(tmp_path, tool=True)
    (template / "tt" / "tt_tool.py").write_text(
        "import sys, time\n"
        "print('hardening line 1', flush=True)\n"
        "time.sleep(30)\n", encoding="utf-8")
    pdk = tmp_path / "pdk"
    pdk.mkdir()
    monkeypatch.setenv("PDK_ROOT", str(pdk))
    monkeypatch.setenv("PDK", "sky130A")
    monkeypatch.setattr("tapeout.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("tapeout._find_librelane", lambda config: "/usr/bin/librelane")
    config = TinyTapeoutConfig(template_dir=template, build_root=tmp_path / "build", timeout_s=60)
    manager = JobManager(tmp_path / "jobs.db", tmp_path / "jobs")

    async def exercise():
        task = asyncio.create_task(run_tiny_tapeout_hardening_job(
            WRAPPER, "demo", config=config, manager=manager))
        job = await _wait_for_job_process(manager)
        for _ in range(100):
            if any("hardening line 1" in line for _, line in job.ring):
                break
            await asyncio.sleep(0.01)
        assert any("hardening line 1" in line for _, line in job.ring)
        assert await manager.cancel(job.id) is True
        result = await task
        assert "CANCELLED" in result
        assert job.status == "cancelled"
        assert job.proc is None
        await manager.stop()

    asyncio.run(exercise())
