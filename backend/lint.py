"""Structured lint diagnostics for write_and_check_syntax (Task 5).

Each supported linter has a parser that converts its native output into a
list of diagnostic dicts:

    {"file": str|None, "line": int|None, "col": int|None, "code": str|None,
     "severity": "error"|"warning", "msg": str}

build_result() then normalizes them into the stable schema the agent sees
(JSON text returned by the tool):

    {"tool", "tool_version", "ok", "verdict", "code_sha256", "saved_to",
     "errors": [...], "errors_total", "warnings_total", "truncated",
     "raw_stderr_tail"}

verdict is one of "ok" | "lint_warning" | "syntax_error" | "elaboration_error";
ok is True for "ok" and "lint_warning" (warnings don't block simulation).

Tool chain priority: slang (--diag-json) > verilator (--diagnostics-sarif on
>= 5.038, text regex below) > iverilog (text) > yosys (text).

Normalization rules: dedupe identical diagnostics (msg gets a " (xN)" suffix),
messages capped at MSG_CAP chars by cutting the middle (bit counts at the ends
survive), at most MAX_DIAGS entries (errors first, source order within a
severity), file/line/col/code are never truncated. Caret snippets are read
back from the written .v file.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess

logger = logging.getLogger("eda")

LINTERS = ("slang", "verilator", "iverilog", "yosys")

MAX_DIAGS = 8        # diagnostics shown to the agent
MSG_CAP = 240        # per-diagnostic message length cap (middle-truncated)
RAW_TAIL_CAP = 1500  # raw_stderr_tail size

# Errors matching this are classified as syntax errors; anything else with
# severity "error" is an elaboration/semantic error.
_SYNTAX_RE = re.compile(r"\bsyntax error\b|\bunexpected\b|\bmalformed\b", re.I)


def pick_linter() -> str | None:
    """First available linter in priority order (detected at call time)."""
    for tool in LINTERS:
        if shutil.which(tool):
            return tool
    return None


_tool_versions: dict[str, str] = {}


def tool_version(tool: str) -> str:
    """First line of the tool's --version/-V output, memoized per binary
    ("" when the version call itself fails)."""
    if tool in _tool_versions:
        return _tool_versions[tool]
    flag = "-V" if tool == "iverilog" else "--version"
    try:
        proc = subprocess.run([tool, flag], capture_output=True, text=True, timeout=10)
        version = (proc.stdout or proc.stderr).strip().splitlines()[0].strip()
    except (OSError, subprocess.TimeoutExpired, IndexError):
        version = ""
    _tool_versions[tool] = version
    return version


def verilator_supports_sarif(version_line: str) -> bool:
    """--diagnostics-sarif exists in Verilator >= 5.038."""
    m = re.search(r"(\d+)\.(\d+)", version_line)
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (5, 38)


# -- parsers -----------------------------------------------------------------

_VERILATOR_LINE = re.compile(
    r"^%(Error|Warning)(?:-([A-Za-z0-9_]+))?:\s*(\S+?):(\d+):(\d+):\s*(.*)$")
_VERILATOR_BARE = re.compile(r"^%(Error|Warning)(?:-([A-Za-z0-9_]+))?:\s*(.*)$")


def parse_verilator_text(output: str) -> list[dict]:
    """Parse verilator's classic text diagnostics:
    %Error[-CODE]: file.v:line:col: msg   (%Warning-CODE: ... likewise).
    Location-less %Error lines (e.g. 'Cannot find file') are kept with null
    location; the 'Exiting due to N error(s)' summary line is dropped."""
    diags = []
    for line in output.splitlines():
        m = _VERILATOR_LINE.match(line)
        if m:
            sev, code, path, lineno, col, msg = m.groups()
        else:
            m = _VERILATOR_BARE.match(line)
            if not m:
                continue
            sev, code, msg = m.groups()
            path = lineno = col = None
            if msg.startswith("Exiting due to"):
                continue
        diags.append({
            "file": path,
            "line": int(lineno) if lineno else None,
            "col": int(col) if col else None,
            "code": code,
            "severity": "error" if sev == "Error" else "warning",
            "msg": msg,
        })
    return diags


def parse_verilator_sarif(text: str) -> list[dict]:
    """Parse verilator's SARIF (--diagnostics-sarif-output) JSON."""
    try:
        data = json.loads(text)
        results = data["runs"][0].get("results") or []
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return []
    diags = []
    for r in results:
        msg = (r.get("message") or {}).get("text") or ""
        # text's first line is the diagnostic; trailers start with "..."
        msg = msg.split("\n")[0]
        file = line_no = col = None
        try:
            loc = r["locations"][0]["physicalLocation"]
            uri = (loc.get("artifactLocation") or {}).get("uri") or ""
            file = re.sub(r"^file://", "", uri) or None
            region = loc.get("region") or {}
            line_no = region.get("startLine")
            col = region.get("startColumn")
        except (KeyError, IndexError, TypeError):
            pass
        diags.append({
            "file": file,
            "line": line_no,
            "col": col,
            "code": r.get("ruleId"),
            "severity": "error" if r.get("level") == "error" else "warning",
            "msg": msg,
        })
    return diags


_SLANG_LOC = re.compile(r"^(.*):(\d+):(\d+)$")


def parse_slang_json(text: str) -> list[dict]:
    """Parse slang's --diag-json output: a JSON array of objects with
    severity/message/code plus an optional 'file:line:col' location string
    and a warning 'optionName' (preferred as the diagnostic code)."""
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    diags = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sev_raw = str(item.get("severity", "")).lower()
        if sev_raw not in ("error", "fatal", "warning"):
            continue  # notes/ignored diagnostics are attached as sub-entries
        file = line_no = col = None
        m = _SLANG_LOC.match(str(item.get("location", "")))
        if m:
            file, line_no, col = m.group(1), int(m.group(2)), int(m.group(3))
        diags.append({
            "file": file,
            "line": line_no,
            "col": col,
            "code": item.get("optionName") or item.get("code") or None,
            "severity": "warning" if sev_raw == "warning" else "error",
            "msg": str(item.get("message", "")),
        })
    return diags


_IVERILOG_LINE = re.compile(r"^(.+?):(\d+):\s*(?:(error|warning|sorry):\s*)?(.*)$", re.I)


def parse_iverilog_text(output: str) -> list[dict]:
    """Parse iverilog diagnostics: 'file.v:N: [error:|warning:] msg'.
    A bare 'file.v:N: syntax error' (no severity keyword) is an error.
    'I give up.' and other non-located lines are dropped."""
    diags = []
    for line in output.splitlines():
        m = _IVERILOG_LINE.match(line)
        if not m:
            continue
        path, lineno, sev, msg = m.groups()
        if sev and sev.lower() == "sorry":
            continue  # feature-limitation notice, not a defect in the design
        diags.append({
            "file": path,
            "line": int(lineno),
            "col": None,
            "code": None,
            "severity": "warning" if (sev or "").lower() == "warning" else "error",
            "msg": msg,
        })
    return diags


_YOSYS_LINE = re.compile(r"^(ERROR|Warning):\s*(.*)$")
_YOSYS_FILE_LINE = re.compile(r"(\S+\.[s]?v):(\d+)")
_YOSYS_LINE_ONLY = re.compile(r"\bline (\d+)\b")


def parse_yosys_text(output: str, default_file: str | None = None) -> list[dict]:
    """Parse yosys 'ERROR: ...' / 'Warning: ...' lines. Location is extracted
    from 'file.v:N' or 'in line N' inside the message when present; yosys
    diagnostics frequently carry no location at all."""
    diags = []
    for line in output.splitlines():
        m = _YOSYS_LINE.match(line)
        if not m:
            continue
        sev, msg = m.groups()
        file, line_no = default_file, None
        fl = _YOSYS_FILE_LINE.search(msg)
        if fl:
            file, line_no = fl.group(1), int(fl.group(2))
        else:
            lo = _YOSYS_LINE_ONLY.search(msg)
            if lo:
                line_no = int(lo.group(1))
        diags.append({
            "file": file,
            "line": line_no,
            "col": None,
            "code": None,
            "severity": "error" if sev == "ERROR" else "warning",
            "msg": msg,
        })
    return diags


# -- normalization -------------------------------------------------------------

def truncate_msg(msg: str, cap: int = MSG_CAP) -> str:
    """Cap a message by cutting the MIDDLE: lint messages carry the numbers
    that matter (bit counts, signal names) at both ends."""
    msg = " ".join(msg.split())
    if len(msg) <= cap:
        return msg
    keep = cap - 3
    head = keep - keep // 2
    return msg[:head] + " … " + msg[-(keep // 2):]


def _snippet(verilog_path: str, line: int | None, col: int | None) -> str | None:
    """'43 | q <= acc;' plus a caret under column *col*, read back from the
    written source file. None when the line can't be located."""
    if line is None:
        return None
    try:
        with open(verilog_path) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    if not 1 <= line <= len(lines):
        return None
    src = lines[line - 1]
    text = f"{line} | {src}"
    if col is not None and col >= 1:
        text += "\n" + " " * len(str(line)) + " | " + " " * (col - 1) + "^"
    return text


def _classify(diags: list[dict], returncode: int) -> str:
    errors = [d for d in diags if d["severity"] == "error"]
    if errors:
        if any(_SYNTAX_RE.search(d["msg"]) for d in errors):
            return "syntax_error"
        return "elaboration_error"
    if returncode != 0:
        # Exit code failed but no diagnostic parsed — the raw tail carries it.
        return "elaboration_error"
    if diags:
        return "lint_warning"
    return "ok"


def build_result(tool: str, tool_version_str: str, verilog_code: str,
                 verilog_path: str, diags: list[dict], returncode: int,
                 raw_output: str, *, policy=None, top_module: str | None = None) -> dict:
    """Normalize parsed diagnostics into the agent-facing result dict.

    ``policy`` is optional to preserve the original result contract. When a
    project-owned policy is supplied, existing keys remain intact and additive
    rule/policy fields record the production gate and applied waivers.
    """
    # Dedupe identical repeats (multiplicity preserved for the totals).
    merged: list[dict] = []
    index: dict[tuple, dict] = {}
    for d in diags:
        key = (d["file"], d["line"], d["col"], d["code"], d["severity"], d["msg"])
        if key in index:
            index[key]["_n"] += 1
        else:
            entry = dict(d, _n=1)
            index[key] = entry
            merged.append(entry)

    errors_total = sum(d["_n"] for d in merged if d["severity"] == "error")
    warnings_total = sum(d["_n"] for d in merged if d["severity"] == "warning")

    # Errors first, source order within each severity, capped.
    merged.sort(key=lambda d: 0 if d["severity"] == "error" else 1)
    shown, truncated = merged[:MAX_DIAGS], len(merged) > MAX_DIAGS

    out_diags = []
    for d in shown:
        msg = truncate_msg(d["msg"])
        if d["_n"] > 1:
            msg = f"{msg} (×{d['_n']})"
        out = {
            "file": d["file"],
            "line": d["line"],
            "col": d["col"],
            "code": d["code"],
            "severity": d["severity"],
            "msg": msg,
        }
        # Snippets only make sense for the file we actually wrote.
        if d["file"] is None or os.path.realpath(str(d["file"])) == os.path.realpath(verilog_path):
            snippet = _snippet(verilog_path, d["line"], d["col"])
            if snippet:
                out["snippet"] = snippet
        out_diags.append(out)

    verdict = _classify(diags, returncode)
    result = {
        "tool": tool,
        "tool_version": tool_version_str,
        "ok": verdict in ("ok", "lint_warning"),
        "verdict": verdict,
        "code_sha256": hashlib.sha256(verilog_code.encode()).hexdigest(),
        "saved_to": verilog_path,
        "errors": out_diags,
        "errors_total": errors_total,
        "warnings_total": warnings_total,
        "truncated": truncated,
        # Only useful when the parser found nothing but the run failed.
        "raw_stderr_tail": raw_output[-RAW_TAIL_CAP:] if (returncode != 0 and not diags) else "",
    }
    if policy is not None:
        # Lazy import keeps lint.py usable as a standalone parser module and
        # avoids coupling the parser to Pydantic when policy is not requested.
        from verification_policy import apply_lint_policy
        result = apply_lint_policy(result, policy, top_module=top_module)
    return result
