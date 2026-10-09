from pathlib import Path
import json
import subprocess

from netlist_equivalence import (
    run_netlist_equivalence, run_synthesis_transform_equivalence,
)


def _inputs(tmp_path, *, candidate=None, library=None):
    reference = tmp_path / "reference.v"
    reference.write_text("module top(input a, output y); assign y=a; endmodule\n")
    candidate_path = tmp_path / "candidate.v"
    candidate_path.write_text(candidate or "module top(input a, output y); assign y=a; endmodule\n")
    library_path = tmp_path / "cells.v"
    library_path.write_text(library or "module BUF(input A, output Y); assign Y=A; endmodule\n")
    return reference, candidate_path, library_path


def test_generates_separate_gold_gate_script_and_binds_digests(tmp_path):
    reference, candidate, library = _inputs(
        tmp_path,
        candidate="module top(input a, output y); BUF u0(.A(a), .Y(y)); endmodule\n",
    )
    seen = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs["cwd"]
        script = (tmp_path / "work" / "equivalence.ys").read_text()
        assert "design -stash gold" in script
        assert "design -stash gate" in script
        assert "read_verilog -formal -sv lib/cells.v" in script
        return subprocess.CompletedProcess(argv, 0, "Equivalence successfully proven\n", "")

    result = run_netlist_equivalence(reference, candidate, "top", library,
                                     destination=tmp_path / "work", runner=runner)
    assert result["status"] == "proven"
    assert result["ok"] is True
    assert len(result["input_sha256"]) == 3
    assert seen["argv"][:3] == ["yosys", "-Q", "-s"]
    saved = json.loads((tmp_path / "work" / "equivalence-result.json").read_text())
    assert saved["report_digest"] == result["report_digest"]


def test_unresolved_cells_fail_closed_without_running_yosys(tmp_path):
    reference, candidate, library = _inputs(
        tmp_path,
        candidate="module top(input a, output y); UNKNOWN u0(.A(a), .Y(y)); endmodule\n",
    )
    called = []
    result = run_netlist_equivalence(reference, candidate, "top", library,
                                     destination=tmp_path / "work",
                                     runner=lambda *args, **kwargs: called.append(args))
    assert result["status"] == "inconclusive"
    assert result["unresolved_cells"] == ["UNKNOWN"]
    assert called == []


def test_blackbox_marker_is_not_a_pass(tmp_path):
    reference, candidate, library = _inputs(
        tmp_path,
        candidate="(* black_box *) module top(input a, output y); endmodule\n",
    )
    result = run_netlist_equivalence(reference, candidate, "top", library,
                                     destination=tmp_path / "work")
    assert result["status"] == "inconclusive"
    assert result["ok"] is False
    assert "blackbox" in result["message"]


def test_missing_input_is_inconclusive_and_durable(tmp_path):
    reference, candidate, library = _inputs(tmp_path)
    candidate.unlink()
    result = run_netlist_equivalence(reference, candidate, "top", library,
                                     destination=tmp_path / "work")
    assert result["status"] == "inconclusive"
    assert result["command"] == []
    assert (tmp_path / "work" / "equivalence-result.json").is_file()


def test_unproven_and_timeout_are_inconclusive_not_green(tmp_path):
    reference, candidate, library = _inputs(tmp_path)

    def failed(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "Found unproven $equiv cells\n", "")

    result = run_netlist_equivalence(reference, candidate, "top", library,
                                     destination=tmp_path / "failed", runner=failed)
    assert result["status"] == "inconclusive"
    assert result["ok"] is False
    assert result["unproven_count"] is None

    def timed(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)

    result = run_netlist_equivalence(reference, candidate, "top", library,
                                     destination=tmp_path / "timeout", runner=timed)
    assert result["status"] == "inconclusive"
    assert result["returncode"] == 124



def test_unproven_count_is_parsed_without_calling_it_a_counterexample(tmp_path):
    reference, candidate, library = _inputs(tmp_path)

    def unproven(*args, **kwargs):
        output = (
            "Found 373 $equiv cells in equiv:\n"
            "Found a total of 145 unproven $equiv cells.\n"
            "ERROR: Found 145 unproven $equiv cells.\n"
        )
        return subprocess.CompletedProcess(args[0], 1, output, "")

    result = run_netlist_equivalence(
        reference, candidate, "top", library,
        destination=tmp_path / "unproven", runner=unproven,
    )
    assert result["status"] == "inconclusive"
    assert result["ok"] is False
    assert result["unproven_count"] == 145
    assert result["message"] == "equivalence left comparison cells unproven"


def test_explicit_counterexample_is_failed(tmp_path):
    reference, candidate, library = _inputs(tmp_path)

    def counterexample(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "counterexample found\n", "")

    result = run_netlist_equivalence(
        reference, candidate, "top", library,
        destination=tmp_path / "counterexample", runner=counterexample,
    )
    assert result["status"] == "failed"
    assert result["ok"] is False


def test_liberty_library_is_staged_and_loaded_with_read_liberty(tmp_path):
    reference, candidate, _ = _inputs(
        tmp_path,
        candidate="module top(input a, output y); sky130_fd_sc_hd__buf_4 u0(.A(a), .Y(y)); endmodule\n",
    )
    liberty = tmp_path / "cells.lib"
    liberty.write_text(
        "library(test) {\n"
        "  cell (\"sky130_fd_sc_hd__buf_4\") {\n"
        "    pin(A) { direction : input; }\n"
        "    pin(Y) { direction : output; }\n"
        "  }\n"
        "}\n"
    )

    def runner(argv, **kwargs):
        work = Path(kwargs["cwd"])
        script = (work / "equivalence.ys").read_text()
        assert "read_liberty -ignore_miss_func lib/cells.lib" in script
        assert "read_liberty -lib lib/cells.lib" not in script
        assert "read_verilog -formal -sv lib/cells.lib" not in script
        assert (work / "lib/cells.lib").is_file()
        return subprocess.CompletedProcess(argv, 0, "Equivalence successfully proven\n", "")

    result = run_netlist_equivalence(
        reference, candidate, "top", liberty,
        destination=tmp_path / "work", runner=runner,
    )
    assert result["status"] == "proven"
    assert result["cell_library_format"] == "liberty"


def test_unlisted_liberty_cell_fails_closed_before_yosys(tmp_path):
    reference, _, _ = _inputs(tmp_path)
    candidate = tmp_path / "candidate.v"
    candidate.write_text(
        "module top(input a, output y); MISSING_CELL u0(.A(a), .Y(y)); endmodule\n"
    )
    liberty = tmp_path / "cells.lib"
    liberty.write_text("library(test) { cell (BUF_X1) { } }\n")
    called = []

    result = run_netlist_equivalence(
        reference, candidate, "top", liberty,
        destination=tmp_path / "work",
        runner=lambda *args, **kwargs: called.append(args),
    )
    assert result["status"] == "inconclusive"
    assert result["unresolved_cells"] == ["MISSING_CELL"]
    assert called == []



def test_async_ff_sat_method_limit_is_inconclusive(tmp_path):
    reference, candidate, library = _inputs(tmp_path)
    output = (
        "ERROR: No SAT model available for async FF cell $adff. "
        "Consider async2sync or clk2fflogic.\n"
    )

    def limited(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, output, "")

    result = run_netlist_equivalence(
        reference, candidate, "top", library,
        destination=tmp_path / "async-method", runner=limited,
    )
    assert result["status"] == "inconclusive"
    assert result["ok"] is False



def test_symmetric_async_reset_abstraction_is_explicit_and_digest_bound(tmp_path):
    reference, candidate, library = _inputs(tmp_path)

    def runner(argv, **kwargs):
        script = (Path(kwargs["cwd"]) / "equivalence.ys").read_text()
        assert script.count("async2sync") == 2
        assert script.count("memory_map") == 2
        assert "rename -top gold" in script
        assert "rename -top gate" in script
        return subprocess.CompletedProcess(
            argv, 0, "Equivalence successfully proven\n", "")

    result = run_netlist_equivalence(
        reference, candidate, "top", library,
        destination=tmp_path / "reset-abstracted",
        async_reset_mode="synchronous_abstraction", runner=runner,
    )
    assert result["status"] == "proven"
    assert result["async_reset_mode"] == "synchronous_abstraction"
    assert "between clock edges is outside" in result["proof_scope"]


def test_preprocessor_macro_named_blackbox_is_not_a_declaration(tmp_path):
    reference = tmp_path / "reference.v"
    candidate = tmp_path / "candidate.v"
    library = tmp_path / "cells.v"
    reference.write_text(
        "`ifndef RISCV_FORMAL_BLACKBOX_REGS\nmodule top; endmodule\n`endif\n")
    candidate.write_text("module top; endmodule\n")
    library.write_text("module cell_model; endmodule\n")

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, "Equivalence successfully proven\n", "")

    result = run_netlist_equivalence(
        reference, candidate, "top", library,
        destination=tmp_path / "macro-name", runner=runner,
    )
    assert result["status"] == "proven"



def test_transform_equivalence_stages_and_digest_binds_exact_script(tmp_path):
    reference, _, _ = _inputs(tmp_path)
    transform = tmp_path / "transform.ys"
    transform.write_text("proc\nopt_clean\n")

    def runner(argv, **kwargs):
        work = Path(kwargs["cwd"])
        wrapper = (work / "equivalence.ys").read_text()
        assert "equiv_opt -assert -undef script transform.ys" in wrapper
        assert (work / "transform.ys").read_text() == "proc\nopt_clean\n"
        return subprocess.CompletedProcess(
            argv, 0, "Equivalence successfully proven\n", "")

    result = run_synthesis_transform_equivalence(
        reference, "top", transform,
        destination=tmp_path / "proof", runner=runner,
    )
    assert result["schema"] == "synthesis-transform-equivalence/v1"
    assert result["status"] == "proven"
    assert set(result["input_sha256"]) == {"reference", "transform_script"}


def test_transform_equivalence_rejects_boundary_commands_before_yosys(tmp_path):
    reference, _, _ = _inputs(tmp_path)
    transform = tmp_path / "transform.ys"
    transform.write_text("proc\nread_verilog replacement.v\n")
    called = []

    result = run_synthesis_transform_equivalence(
        reference, "top", transform,
        destination=tmp_path / "proof",
        runner=lambda *args, **kwargs: called.append(args),
    )
    assert result["status"] == "inconclusive"
    assert result["forbidden_command"] == "read_verilog"
    assert called == []


def test_transform_equivalence_unproven_is_inconclusive(tmp_path):
    reference, _, _ = _inputs(tmp_path)
    transform = tmp_path / "transform.ys"
    transform.write_text("synth -flatten -top top\n")

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, "ERROR: Found 7 unproven $equiv cells.\n", "")

    result = run_synthesis_transform_equivalence(
        reference, "top", transform,
        destination=tmp_path / "proof", runner=runner,
    )
    assert result["status"] == "inconclusive"
    assert result["unproven_count"] == 7



def test_terminal_success_overrides_intermediate_induction_workset(tmp_path):
    reference, _, _ = _inputs(tmp_path)
    transform = tmp_path / "transform.ys"
    transform.write_text("opt_clean\n")
    output = (
        "Found 1 unproven $equiv cells in module equiv:\n"
        "Proved 1 previously unproven $equiv cells.\n"
        "Of those cells 1 are proven and 0 are unproven.\n"
        "Equivalence successfully proven!\n"
    )

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, output, "")

    result = run_synthesis_transform_equivalence(
        reference, "top", transform,
        destination=tmp_path / "proof", runner=runner,
    )
    assert result["status"] == "proven"
    assert result["unproven_count"] == 0
