"""Fail-closed equivalence gate for technology-mapped netlists.

This is intentionally a small execution boundary.  It does not run an EDA
flow on import and it never treats missing inputs, unresolved cells, or a
black-box model as a passing result.  The caller supplies the RTL/reference,
candidate netlist, and the cell model/library used to parse the candidate.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping


SCHEMA = "netlist-equivalence/v1"
TRANSFORM_SCHEMA = "synthesis-transform-equivalence/v1"
_TOP = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_MODULE = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", re.I)
_LIBERTY_CELL = re.compile(r"(?m)^\s*cell\s*\(\s*[\x22]?(?P<name>[A-Za-z_][A-Za-z0-9_$]*)[\x22]?\s*\)")
_INSTANCE = re.compile(
    r"(?m)(?:^|;)\s*(?P<type>[A-Za-z_][A-Za-z0-9_$]*)\s*"
    r"(?:#\s*\(.*?\)\s*)?"
    r"(?P<instance>[A-Za-z_][A-Za-z0-9_$]*)\s*\(", re.S,
)
_KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "wire", "tri",
    "logic", "reg", "integer", "genvar", "assign", "always", "always_ff",
    "always_comb", "initial", "if", "else", "for", "case", "begin", "end",
    "function", "task", "generate", "endgenerate", "parameter", "localparam",
    "specify", "endspecify", "primitive", "endprimitive", "and", "or", "xor",
    "xnor", "nand", "nor", "not", "buf", "bufif0", "bufif1", "notif0", "notif1",
    "cmos", "rcmos", "tran", "rtran", "tranif0", "tranif1", "rtranif0", "rtranif1",
    "pullup", "pulldown", "supply0", "supply1", "wire", "wand", "wor", "uwire",
}


class NetlistEquivalenceError(ValueError):
    """Raised for malformed invocation arguments, before an EDA process."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _regular_file(value: str | os.PathLike[str], label: str) -> tuple[Path | None, str | None]:
    path = Path(value).expanduser()
    if not path.is_file() or path.is_symlink():
        return None, f"{label} is missing or is not a regular file"
    try:
        path.resolve(strict=True)
    except OSError:
        return None, f"{label} cannot be resolved"
    return path, None


def _modules(text: str) -> set[str]:
    return set(_MODULE.findall(text))


def _liberty_cells(text: str) -> set[str]:
    """Return cell declarations from a Liberty source.

    Liberty is not Verilog, so looking for ``module`` declarations would
    incorrectly report every mapped cell as unresolved. Syntax and semantic
    validation remains the responsibility of Yosys.
    """
    return set(_LIBERTY_CELL.findall(text))


def _is_liberty(path: Path, text: str) -> bool:
    """Identify a Liberty cell library without trusting the suffix alone."""
    if path.suffix.lower() in {".lib", ".liberty"}:
        return True
    # Requiring both constructs avoids treating an arbitrary Verilog file
    # containing the word ``cell`` as Liberty.
    return bool(re.search(r"(?m)^\s*library\s*\(", text)) and bool(_liberty_cells(text))



def _instantiated_types(text: str) -> set[str]:
    return {
        match.group("type") for match in _INSTANCE.finditer(text)
        if match.group("type").lower() not in _KEYWORDS
    }


def _blackbox_markers(text: str) -> list[str]:
    markers: list[str] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        # Macro names can select optional implementations; a preprocessor
        # directive is not itself a Yosys black-box declaration.
        if line.lstrip().startswith("`"):
            continue
        if re.search(r"black[_ ]?box|blackbox", line, re.I):
            markers.append(f"line {line_no}: {line.strip()[:180]}")
    return markers[:8]


def _result(
    *, status: str, destination: Path, message: str, **values: Any,
) -> dict[str, Any]:
    if status not in {"proven", "failed", "inconclusive"}:
        raise ValueError(f"invalid equivalence status: {status}")
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "status": status,
        "ok": status == "proven",
        "message": message,
        **values,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False).encode()
    payload["report_digest"] = _sha(canonical)
    _atomic_write(destination / "equivalence-result.json",
                  (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())
    return payload


def _status(output: str, returncode: int) -> str:
    # Yosys method limitation, not a functional counterexample: async FF
    # cells need an explicit semantic transformation/assumption before SAT.
    # Do not apply async2sync automatically; it changes asynchronous reset
    # behavior and is not generally equivalent without reset assumptions.
    if re.search(r"no SAT model available|consider async2sync|async FF cell", output, re.I):
        return "inconclusive"
    if re.search(r"counterexample|assert(?:ion)?.*fail|proof.*model found.*fail",
                 output, re.I):
        return "failed"
    # Induction logs first describe their workset as unproven, then prove it.
    # A successful terminal status from a zero-exit Yosys run takes precedence
    # over those intermediate counts.
    if returncode == 0 and re.search(
            r"equivalence\s+(?:successfully\s+)?proven|all\s+.*equivalent|equiv_status.*pass",
            output, re.I):
        return "proven"
    # `equiv_status -assert` exits non-zero when cells remain unproven.
    # That is a proof limitation, not a demonstrated functional counterexample.
    if re.search(r"(?:found a total of|found)\s+\d+\s+unproven\s+\$equiv\s+cells",
                 output, re.I):
        return "inconclusive"
    return "inconclusive"


def _unproven_count(output: str) -> int | None:
    matches = re.findall(
        r"(?:found a total of|found)\s+(\d+)\s+unproven\s+\$equiv\s+cells"
        r"|of those cells\s+\d+\s+are proven and\s+(\d+)\s+are unproven",
        output,
        re.I,
    )
    if not matches:
        return None
    first, second = matches[-1]
    return int(first or second)


def _script(top_module: str, *, reference: str, candidate: str, library: str,
            induction_cycles: int, liberty: bool = False,
            async_reset_mode: str = "preserve") -> str:
    # Paths are staged below the work directory, so no shell/path quoting is
    # needed and the runner can execute with cwd=work.
    library_read = (f"read_liberty -ignore_miss_func {library}" if liberty else
                    f"read_verilog -formal -sv {library}")
    reset_transform = ("async2sync",) if async_reset_mode == "synchronous_abstraction" else ()
    return "\n".join((
        library_read,
        f"read_verilog -formal -sv {reference}",
        f"prep -top {top_module} -flatten",
        "memory_map",
        *reset_transform,
        "opt_clean",
        "rename -top gold",
        "design -stash gold",
        "design -reset",
        library_read,
        f"read_verilog -formal -sv {candidate}",
        f"prep -top {top_module} -flatten",
        "memory_map",
        *reset_transform,
        "opt_clean",
        "rename -top gate",
        "design -stash gate",
        "design -reset",
        "design -copy-from gold -as gold gold",
        "design -copy-from gate -as gate gate",
        "equiv_make gold gate equiv",
        "hierarchy -top equiv",
        "equiv_simple",
        f"equiv_induct -seq {induction_cycles}",
        "equiv_status -assert",
        "",
    ))


def run_netlist_equivalence(
    reference_netlist: str | os.PathLike[str],
    candidate_netlist: str | os.PathLike[str],
    top_module: str,
    cell_library: str | os.PathLike[str],
    *,
    destination: str | os.PathLike[str],
    yosys: str = "yosys",
    timeout_s: float = 900,
    induction_cycles: int = 20,
    async_reset_mode: str = "preserve",
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Compare a reference design with a mapped/routed candidate.

    Inputs are copied into the durable work directory and all result hashes
    refer to those exact bytes.  The default runner is ``subprocess.run``;
    tests can inject a runner without executing Yosys.
    """
    if not _TOP.fullmatch(top_module):
        raise NetlistEquivalenceError("top_module is not a portable identifier")
    if timeout_s <= 0 or induction_cycles < 1:
        raise NetlistEquivalenceError("timeout_s and induction_cycles must be positive")
    if async_reset_mode not in {"preserve", "synchronous_abstraction"}:
        raise NetlistEquivalenceError(
            "async_reset_mode must be preserve or synchronous_abstraction")
    work = Path(destination).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    sources: dict[str, Path] = {}
    errors: list[str] = []
    for key, value in (("reference", reference_netlist), ("candidate", candidate_netlist),
                       ("cell_library", cell_library)):
        path, error = _regular_file(value, key)
        if error:
            errors.append(error)
        elif path is not None:
            sources[key] = path
    if errors:
        return _result(status="inconclusive", destination=work,
                       message="; ".join(errors), top_module=top_module,
                       input_errors=errors, command=[])

    texts = {key: path.read_text(encoding="utf-8", errors="replace")
             for key, path in sources.items()}
    for key, text in texts.items():
        markers = _blackbox_markers(text)
        if markers:
            return _result(status="inconclusive", destination=work,
                           message=f"{key} contains blackbox markers",
                           top_module=top_module, blackbox_markers={key: markers},
                           command=[])
    gate_types = _instantiated_types(texts["candidate"])
    liberty = _is_liberty(sources["cell_library"], texts["cell_library"])
    known_types = _modules(texts["candidate"])
    known_types |= (_liberty_cells(texts["cell_library"])
                    if liberty else _modules(texts["cell_library"]))
    unresolved = sorted(gate_types - known_types)
    if unresolved:
        return _result(status="inconclusive", destination=work,
                       message="candidate contains unresolved cell/module types",
                       top_module=top_module, unresolved_cells=unresolved,
                       command=[])
    library_name = "lib/cells.lib" if liberty else "lib/cells.v"
    staged = {"gold/reference.v": texts["reference"].encode(),
              "gate/candidate.v": texts["candidate"].encode(),
              library_name: texts["cell_library"].encode()}
    for relative, body in staged.items():
        _atomic_write(work / relative, body)
    script_text = _script(top_module, reference="gold/reference.v",
                          candidate="gate/candidate.v", library=library_name,
                          induction_cycles=induction_cycles, liberty=liberty,
                          async_reset_mode=async_reset_mode)
    _atomic_write(work / "equivalence.ys", script_text.encode())
    hashes = {
        key: _sha(path.read_bytes()) for key, path in sources.items()
    }
    argv = [yosys, "-Q", "-s", "equivalence.ys", "-l", "equivalence.log"]
    execute = runner or subprocess.run
    try:
        process = execute(argv, cwd=str(work), capture_output=True, text=True,
                          check=False, timeout=timeout_s)
        returncode = int(getattr(process, "returncode", 1))
        output = ((getattr(process, "stdout", "") or "") +
                  (getattr(process, "stderr", "") or ""))
    except subprocess.TimeoutExpired as exc:
        returncode, output = 124, f"equivalence timed out: {exc}"
    except (OSError, subprocess.SubprocessError) as exc:
        returncode, output = 127, str(exc)
    log_path = work / "equivalence.log"
    if not log_path.exists():
        _atomic_write(log_path, output.encode())
    else:
        output = log_path.read_text(encoding="utf-8", errors="replace") + output
    status = _status(output, returncode)
    unproven_count = _unproven_count(output)
    return _result(status=status, destination=work,
                   message=("equivalence completed" if status == "proven" else
                            "equivalence left comparison cells unproven"
                            if unproven_count is not None else
                            "equivalence reported a counterexample" if status == "failed" else
                            "equivalence did not produce a conclusive result"),
                   top_module=top_module, returncode=returncode, command=argv,
                   input_sha256=hashes, script_sha256=_sha(script_text.encode()),
                   cell_library_format="liberty" if liberty else "verilog",
                   async_reset_mode=async_reset_mode,
                   proof_scope=(
                       "clock-edge behavior after symmetric async2sync transformation; "
                       "asynchronous reset assertion between clock edges is outside this proof"
                       if async_reset_mode == "synchronous_abstraction" else
                       "native asynchronous-reset semantics"),
                   unproven_count=unproven_count,
                   unresolved_cells=[], blackbox_markers={},
                   output_tail=output[-16000:])


def run_synthesis_transform_equivalence(
    reference_netlist: str | os.PathLike[str],
    top_module: str,
    transform_script: str | os.PathLike[str],
    *,
    destination: str | os.PathLike[str],
    yosys: str = "yosys",
    timeout_s: float = 900,
    undef_aware: bool = True,
    async_reset_mode: str = "preserve",
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Prove a synthesis command sequence against the design it transforms.

    The gold and transformed designs are created inside one Yosys process, so
    unconstrained initial state is not independently elaborated. The exact
    transformation script is staged and digest-bound into the durable result.
    It may only transform the loaded design; boundary-changing commands fail
    closed before Yosys runs.
    """
    if not _TOP.fullmatch(top_module):
        raise NetlistEquivalenceError("top_module is not a portable identifier")
    if timeout_s <= 0:
        raise NetlistEquivalenceError("timeout_s must be positive")
    if async_reset_mode not in {"preserve", "synchronous_abstraction"}:
        raise NetlistEquivalenceError(
            "async_reset_mode must be preserve or synchronous_abstraction")

    work = Path(destination).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    reference, reference_error = _regular_file(reference_netlist, "reference")
    transform, transform_error = _regular_file(transform_script, "transform_script")
    errors = [error for error in (reference_error, transform_error) if error]
    if errors:
        return _result(status="inconclusive", destination=work,
                       message="; ".join(errors), top_module=top_module,
                       schema=TRANSFORM_SCHEMA, input_errors=errors, command=[])
    assert reference is not None and transform is not None
    reference_bytes = reference.read_bytes()
    transform_bytes = transform.read_bytes()
    transform_text = transform_bytes.decode("utf-8", errors="replace")
    forbidden = re.compile(
        r"(?im)^\s*(?:read(?:_verilog|_liberty)?|design|equiv\w*|write\w*|"
        r"shell|plugin|exec|script)\b")
    match = forbidden.search(transform_text)
    if match:
        return _result(
            status="inconclusive", destination=work,
            message="transform script contains a forbidden boundary command",
            top_module=top_module, schema=TRANSFORM_SCHEMA,
            forbidden_command=match.group(0).strip(), command=[])

    _atomic_write(work / "reference.v", reference_bytes)
    _atomic_write(work / "transform.ys", transform_bytes)
    reset_before = ("async2sync",) if async_reset_mode == "synchronous_abstraction" else ()
    flags = " -undef" if undef_aware else ""
    wrapper = "\n".join((
        "read_verilog -formal -sv reference.v",
        f"hierarchy -check -top {top_module}",
        "proc",
        "memory",
        "opt_clean",
        *reset_before,
        f"equiv_opt -assert{flags} script transform.ys",
        "",
    ))
    _atomic_write(work / "equivalence.ys", wrapper.encode())
    argv = [yosys, "-Q", "-s", "equivalence.ys", "-l", "equivalence.log"]
    execute = runner or subprocess.run
    try:
        process = execute(argv, cwd=str(work), capture_output=True, text=True,
                          check=False, timeout=timeout_s)
        returncode = int(getattr(process, "returncode", 1))
        output = ((getattr(process, "stdout", "") or "") +
                  (getattr(process, "stderr", "") or ""))
    except subprocess.TimeoutExpired as exc:
        returncode, output = 124, f"equivalence timed out: {exc}"
    except (OSError, subprocess.SubprocessError) as exc:
        returncode, output = 127, str(exc)
    log_path = work / "equivalence.log"
    if log_path.exists():
        output = log_path.read_text(encoding="utf-8", errors="replace") + output
    else:
        _atomic_write(log_path, output.encode())
    status = _status(output, returncode)
    unproven_count = _unproven_count(output)
    return _result(
        status=status, destination=work,
        message=("transformation equivalence completed" if status == "proven" else
                 "transformation equivalence left comparison cells unproven"
                 if unproven_count is not None else
                 "transformation equivalence reported a counterexample"
                 if status == "failed" else
                 "transformation equivalence did not produce a conclusive result"),
        schema=TRANSFORM_SCHEMA, top_module=top_module, returncode=returncode,
        command=argv,
        input_sha256={"reference": _sha(reference_bytes),
                      "transform_script": _sha(transform_bytes)},
        script_sha256=_sha(wrapper.encode()), undef_aware=undef_aware,
        async_reset_mode=async_reset_mode, unproven_count=unproven_count,
        output_tail=output[-16000:])


__all__ = [
    "SCHEMA", "TRANSFORM_SCHEMA", "NetlistEquivalenceError",
    "run_netlist_equivalence", "run_synthesis_transform_equivalence",
]
