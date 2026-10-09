"""Structural clock/reset-domain crossing analysis.

This module is deliberately conservative.  It consumes the machine-readable
JSON emitted by Yosys and reports only relationships that can be established
from the netlist; it is not a replacement for a timing-aware CDC signoff
tool.  A small Yosys wrapper is provided for callers that have RTL but no
netlist yet.  Missing Yosys is reported as ``inconclusive`` rather than as a
clean result.

The public entry point is :func:`analyze_cdc_rdc`.  It accepts a Yosys JSON
mapping, a JSON file/text value, or RTL (``verilog_code=...``).  Results are
plain, versioned records so they can be persisted independently of the
verification pipeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = "cdc-rdc/v1"
_CLOCK_RE = re.compile(r"(?:^|[_/])(clk|clock|clock_i|i_clk)(?:$|[_/0-9])", re.I)
_RESET_RE = re.compile(r"(?:^|[_/])(rst|reset|areset|arst|reset_n|rst_n)(?:$|[_/0-9])", re.I)
_FF_RE = re.compile(r"(?:dff|ff|flop)", re.I)
_OUTPUT_PINS = {"q", "qn", "y", "o", "out", "z", "x"}
_CLOCK_PINS = ("C", "CLK", "G", "clock", "clk")
_DATA_PINS = ("D", "d", "DATA", "data")
_RESET_PINS = ("ARST", "ARSTN", "RESET", "RST", "RN", "SN", "CLR", "SET", "reset", "rst")


class CdcWaiverScope(BaseModel):
    """The fields on a finding to which a waiver applies.

    At least one field is required so a waiver cannot silently disable the
    entire analysis.  ``signal`` supports a simple glob (``fnmatch``).
    """

    model_config = ConfigDict(extra="forbid")
    rule_id: str | None = None
    category: str | None = None
    top_module: str | None = None
    signal: str | None = None
    source_domain: str | None = None
    destination_domain: str | None = None

    @model_validator(mode="after")
    def bounded(self) -> "CdcWaiverScope":
        if not any(value is not None for value in self.model_dump().values()):
            raise ValueError("CDC/RDC waiver scope must constrain at least one field")
        return self


class CdcWaiver(BaseModel):
    """An attributable, expiring CDC/RDC exception."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    author: str = Field(min_length=1)
    scope: CdcWaiverScope
    expires: date
    ticket: str | None = None

    def active(self, *, on: date | None = None) -> bool:
        return self.expires >= (on or date.today())


class CdcFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    rule_id: str
    category: str
    severity: str
    message: str
    signal: str | None = None
    width: int = Field(default=1, ge=1)
    source_domain: str | None = None
    destination_domain: str | None = None
    cells: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    waived: bool = False
    waiver_id: str | None = None


class CdcRdcReport(BaseModel):
    """Serializable result of one structural CDC/RDC inspection."""

    model_config = ConfigDict(extra="forbid")
    schema_version: str = SCHEMA_VERSION
    run_id: str
    generated_at: datetime
    top_module: str
    status: str
    tool: str
    tool_version: str | None = None
    tool_available: bool = True
    netlist_sha256: str | None = None
    clock_domains: list[dict[str, Any]] = Field(default_factory=list)
    reset_domains: list[dict[str, Any]] = Field(default_factory=list)
    synchronizers: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[CdcFinding] = Field(default_factory=list)
    waivers_applied: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    limitations: list[str] = Field(default_factory=list)
    artifact_path: str | None = None
    artifact_sha256: str | None = None

    @property
    def outcome(self) -> str:
        """Alias used by the verification gate without changing ``status``."""
        return self.status

    @property
    def errors(self) -> list[CdcFinding]:
        return [item for item in self.findings if item.severity == "error" and not item.waived]

    def as_dict(self) -> dict[str, Any]:
        try:
            return self.model_dump(mode="json")
        except AttributeError:  # pydantic 1 compatibility for older worktrees
            return json.loads(self.json())

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, indent=2, default=str) + "\n"


class CdcRdcStore:
    """Durable JSON artifacts for CDC/RDC reports."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root or os.environ.get("CDC_RDC_ARTIFACTS", "backend/data/cdc_rdc"))
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, report: CdcRdcReport) -> Path:
        path = self.root / f"{report.run_id}.json"
        # Hash the canonical report without the artifact fields first.  The
        # final JSON includes the hash/path and is itself then hashed too.
        report.artifact_path = str(path)
        report.artifact_sha256 = None
        initial = report.to_json().encode("utf-8")
        report.artifact_sha256 = hashlib.sha256(initial).hexdigest()
        payload = report.to_json().encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{report.run_id}.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
            try:
                fd_dir = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(fd_dir)
                finally:
                    os.close(fd_dir)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return path

    def load(self, run_id: str) -> CdcRdcReport:
        if Path(run_id).name != run_id or not run_id:
            raise ValueError("invalid CDC/RDC run id")
        return CdcRdcReport.model_validate(json.loads((self.root / f"{run_id}.json").read_text()))


def _dump_model(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        try:
            return value.model_dump(mode="python")
        except AttributeError:
            return json.loads(value.json())
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("expected a CDC/RDC waiver mapping or model")


def _normalise_waivers(waivers: Iterable[CdcWaiver | Mapping[str, Any]] | None) -> list[CdcWaiver]:
    result: list[CdcWaiver] = []
    for item in waivers or ():
        if isinstance(item, CdcWaiver):
            result.append(item)
            continue
        payload = _dump_model(item)
        result.append(CdcWaiver.model_validate(payload))
    return result


def _scope_matches(waiver: CdcWaiver, finding: CdcFinding, top_module: str) -> bool:
    import fnmatch
    scope = waiver.scope
    values = {
        "rule_id": finding.rule_id,
        "category": finding.category,
        "top_module": top_module,
        "signal": finding.signal or "",
        "source_domain": finding.source_domain or "",
        "destination_domain": finding.destination_domain or "",
    }
    for key, expected in scope.model_dump().items():
        if expected is not None and not fnmatch.fnmatch(str(values[key]), str(expected)):
            return False
    return True


def _bit_name(netnames: Mapping[str, Any], bit: Any) -> str | None:
    for name, item in netnames.items():
        bits = item.get("bits", []) if isinstance(item, Mapping) else []
        if bit in bits:
            return name
    return None


def _port_for_bit(ports: Mapping[str, Any], bit: Any) -> tuple[str, Mapping[str, Any]] | None:
    for name, port in ports.items():
        if bit in (port.get("bits", []) if isinstance(port, Mapping) else []):
            return name, port
    return None


def _is_ff(cell_type: str) -> bool:
    return bool(_FF_RE.search(cell_type or "")) or cell_type.lower() in {
        "$dff", "$adff", "$sdff", "$_dff_p_", "$_dff_n_", "$_adff_pp_",
        "$_adff_np_", "$_sdff_pp0_", "$_sdff_pp1_",
    }


def _pin(connections: Mapping[str, Any], candidates: Iterable[str]) -> list[Any]:
    for name in candidates:
        if name in connections:
            value = connections[name]
            return list(value) if isinstance(value, (list, tuple)) else [value]
    return []


def _is_clock_name(name: str) -> bool:
    return bool(_CLOCK_RE.search(name)) or name.lower() in {"clk", "clock", "i_clk"}


def _is_reset_name(name: str) -> bool:
    return bool(_RESET_RE.search(name)) or name.lower() in {"rst", "reset", "rst_n", "reset_n", "arst", "arst_n"}


def _constant_bits(bits: Iterable[Any]) -> bool:
    return bool(list(bits)) and all(str(bit) in {"0", "1", "x", "z"} for bit in bits)


def _analyse_netlist(netlist: Mapping[str, Any], top_module: str, *, tool: str = "yosys-json",
                     tool_version: str | None = None,
                     netlist_sha256: str | None = None,
                     waivers: Iterable[CdcWaiver | Mapping[str, Any]] | None = None) -> CdcRdcReport:
    modules = netlist.get("modules") if isinstance(netlist, Mapping) else None
    if not isinstance(modules, Mapping) or not modules:
        return CdcRdcReport(run_id=f"cdc-{uuid.uuid4().hex}", generated_at=datetime.now(timezone.utc),
                            top_module=top_module, status="inconclusive", tool=tool,
                            tool_version=tool_version, tool_available=True,
                            netlist_sha256=netlist_sha256, confidence="low",
                            limitations=["Yosys JSON has no modules section"])
    if top_module not in modules:
        if len(modules) == 1:
            top_module = next(iter(modules))
        else:
            return CdcRdcReport(run_id=f"cdc-{uuid.uuid4().hex}", generated_at=datetime.now(timezone.utc),
                                top_module=top_module, status="inconclusive", tool=tool,
                                tool_version=tool_version, tool_available=True,
                                netlist_sha256=netlist_sha256, confidence="low",
                                limitations=[f"top module {top_module!r} is absent from Yosys JSON"])
    module = modules[top_module]
    ports = module.get("ports", {}) or {}
    cells = module.get("cells", {}) or {}
    netnames = module.get("netnames", {}) or {}
    bit_ports = {bit: (name, port) for name, port in ports.items()
                 for bit in (port.get("bits", []) if isinstance(port, Mapping) else [])}
    # Locate clock ports first; this anchors all inferred domains.
    clocks: dict[Any, str] = {}
    clock_domains: list[dict[str, Any]] = []
    for name, port in ports.items():
        if isinstance(port, Mapping) and port.get("direction") == "input" and _is_clock_name(name):
            bits = list(port.get("bits", []))
            domain = name
            clock_domains.append({"name": domain, "clock_port": name, "width": len(bits) or 1})
            for bit in bits:
                clocks[bit] = domain

    ff_info: dict[str, dict[str, Any]] = {}
    driver: dict[Any, tuple[str, str]] = {}
    # First collect registers and their Q drivers.  Most Yosys JSON cells use
    # Q/D/C/ARST, but aliases are accepted for mapped primitive cells.
    for cell_name, cell in cells.items():
        if not isinstance(cell, Mapping):
            continue
        ctype = str(cell.get("type", ""))
        conns = cell.get("connections", {}) or {}
        if not _is_ff(ctype):
            continue
        q_bits = _pin(conns, ("Q", "q", "QN", "q_n"))
        clk_bits = _pin(conns, _CLOCK_PINS)
        d_bits = _pin(conns, _DATA_PINS)
        rst_bits = _pin(conns, _RESET_PINS) if any(key in conns for key in _RESET_PINS) else []
        clk = next((clocks.get(bit) for bit in clk_bits if bit in clocks), None)
        if clk is None:
            # A generated clock may not be a top-level port.  Preserve a
            # stable domain token so crossings are still visible.
            clk_name = next((_bit_name(netnames, bit) for bit in clk_bits if _bit_name(netnames, bit)), None)
            clk = clk_name if clk_name and _is_clock_name(clk_name) else (f"clock:{clk_bits[0]}" if clk_bits else "unknown-clock")
            for bit in clk_bits:
                if bit not in clocks:
                    clocks[bit] = clk
            if not any(item["name"] == clk for item in clock_domains):
                clock_domains.append({"name": clk, "clock_port": _bit_name(netnames, clk_bits[0]) if clk_bits else None,
                                      "width": len(clk_bits) or 1})
        info = {"name": cell_name, "type": ctype, "q": q_bits, "d": d_bits,
                "clock": clk, "reset": rst_bits, "connections": conns}
        ff_info[cell_name] = info
        for bit in q_bits:
            driver[bit] = (cell_name, "ff")

    # Add obvious combinational output drivers for one-step/backwards tracing.
    for cell_name, cell in cells.items():
        if not isinstance(cell, Mapping) or cell_name in ff_info:
            continue
        conns = cell.get("connections", {}) or {}
        for pin, bits in conns.items():
            if str(pin).lower() in _OUTPUT_PINS:
                seq = list(bits) if isinstance(bits, (list, tuple)) else [bits]
                for bit in seq:
                    driver.setdefault(bit, (cell_name, "comb"))

    def source_domains(bits: Iterable[Any], seen: set[str] | None = None) -> tuple[set[str], set[str]]:
        seen = seen or set()
        domains: set[str] = set()
        source_cells: set[str] = set()
        for bit in bits:
            if str(bit) in {"0", "1", "x", "z"}:
                continue
            port = bit_ports.get(bit)
            if port and port[1].get("direction") == "input":
                domains.add("unknown")
                source_cells.add(port[0])
                continue
            found = driver.get(bit)
            if not found:
                domains.add("unknown")
                continue
            cell_name, kind = found
            if cell_name in seen:
                domains.add("unknown")
                continue
            seen.add(cell_name)
            if kind == "ff" and cell_name in ff_info:
                domains.add(ff_info[cell_name]["clock"])
                source_cells.add(cell_name)
                continue
            cell = cells.get(cell_name, {})
            conns = cell.get("connections", {}) if isinstance(cell, Mapping) else {}
            for pin, values in conns.items():
                if str(pin).lower() in _OUTPUT_PINS:
                    continue
                seq = list(values) if isinstance(values, (list, tuple)) else [values]
                subdomains, subcells = source_domains(seq, seen)
                domains.update(subdomains)
                source_cells.update(subcells)
        return domains, source_cells

    # Map first-stage Q to successors, then evaluate each register's D input.
    successors: dict[str, set[str]] = {name: set() for name in ff_info}
    for dest_name, dest in ff_info.items():
        for src_name, src in ff_info.items():
            if src_name == dest_name or not src["q"] or not dest["d"]:
                continue
            if set(src["q"]) & set(dest["d"]):
                successors[src_name].add(dest_name)

    synchronizers: list[dict[str, Any]] = []
    for first_name, next_names in successors.items():
        first = ff_info[first_name]
        for second_name in sorted(next_names):
            second = ff_info[second_name]
            if first["clock"] == second["clock"]:
                synchronizers.append({"cells": [first_name, second_name], "domain": first["clock"],
                                      "width": len(first["q"]) or 1, "kind": "two-flop"})

    findings: list[CdcFinding] = []
    finding_seq = 0
    for dest_name, dest in ff_info.items():
        if not dest["d"]:
            continue
        domains, source_cells = source_domains(dest["d"])
        known = {item for item in domains if item not in {dest["clock"], "unknown"}}
        crossing = {item for item in known if item != dest["clock"]}
        if not crossing:
            continue
        # The crossing lands in the first synchronizer stage; its same-clock
        # successor is the second stage.
        synced = any(ff_info.get(successor, {}).get("clock") == dest["clock"]
                     for successor in successors.get(dest_name, ()))
        if synced:
            continue
        source_domain = sorted(crossing)[0] if crossing else None
        width = len(dest["d"]) or 1
        rule = "CDC-MULTIBIT" if width > 1 else ("CDC-EXTERNAL" if source_domain and source_domain.startswith("external:") else "CDC-UNSYNC")
        severity = "warning" if rule == "CDC-EXTERNAL" else "error"
        finding_seq += 1
        signal = _bit_name(netnames, dest["d"][0]) or dest_name
        findings.append(CdcFinding(id=f"CDC-{finding_seq:04d}", rule_id=rule, category="cdc",
                                   severity=severity, signal=signal, width=width,
                                   source_domain=source_domain, destination_domain=dest["clock"],
                                   cells=sorted({dest_name, *source_cells}), confidence="medium",
                                   message=(f"{width}-bit signal crosses {source_domain} -> {dest['clock']} "
                                            "without a recognized two-flop synchronizer")))

    reset_domains: list[dict[str, Any]] = []
    reset_syncs: list[dict[str, Any]] = []
    for domain in sorted({item["clock"] for item in ff_info.values()}):
        regs = [item for item in ff_info.values() if item["clock"] == domain and item["reset"]]
        if not regs:
            continue
        reset_bits = sorted({str(bit) for item in regs for bit in item["reset"]})
        reset_names = sorted({_bit_name(netnames, bit) or (bit_ports.get(bit, (None,))[0] if bit in bit_ports else str(bit))
                              for item in regs for bit in item["reset"]})
        reset_domains.append({"name": domain, "signals": reset_names, "registers": len(regs), "asynchronous": True})
        chains = []
        for first in regs:
            if not _constant_bits(first["d"]):
                continue
            for second in regs:
                if first["name"] == second["name"] or first["clock"] != second["clock"]:
                    continue
                if set(first["q"]) & set(second["d"]) and set(first["reset"]) & set(second["reset"]):
                    chains.append([first["name"], second["name"]])
        if chains:
            for chain in chains:
                reset_syncs.append({"cells": chain, "domain": domain, "kind": "async-assert-sync-release"})
            continue
        finding_seq += 1
        findings.append(CdcFinding(id=f"RDC-{finding_seq:04d}", rule_id="RDC-ASYNC-RESET",
                                   category="rdc", severity="error", signal=reset_names[0] if reset_names else None,
                                   width=1, source_domain="asynchronous-reset", destination_domain=domain,
                                   cells=[item["name"] for item in regs], confidence="low",
                                   message=(f"asynchronous reset in {domain} has no recognized two-flop "
                                            "synchronized-release chain")))
    synchronizers.extend(reset_syncs)

    active_waivers = _normalise_waivers(waivers)
    applied: list[str] = []
    for finding in findings:
        for waiver in active_waivers:
            if waiver.active() and _scope_matches(waiver, finding, top_module):
                finding.waived = True
                finding.waiver_id = waiver.id
                applied.append(waiver.id)
                break
    unwaived_errors = [item for item in findings if item.severity == "error" and not item.waived]
    limitations = [
        "Structural analysis does not prove metastability safety, MTBF, protocol correctness, or timing closure.",
        "Combinational fan-in is traced conservatively; generated clocks and black boxes may be unknown.",
    ]
    confidence = "medium"
    return CdcRdcReport(run_id=f"cdc-{uuid.uuid4().hex}", generated_at=datetime.now(timezone.utc),
                        top_module=top_module, status="fail" if unwaived_errors else "pass", tool=tool,
                        tool_version=tool_version, tool_available=True, netlist_sha256=netlist_sha256,
                        clock_domains=clock_domains, reset_domains=reset_domains,
                        synchronizers=synchronizers, findings=findings,
                        waivers_applied=sorted(set(applied)), confidence=confidence,
                        limitations=limitations)


def _inconclusive(top_module: str, reason: str, *, tool_version: str | None = None,
                  netlist_sha256: str | None = None) -> CdcRdcReport:
    return CdcRdcReport(run_id=f"cdc-{uuid.uuid4().hex}", generated_at=datetime.now(timezone.utc),
                        top_module=top_module, status="inconclusive", tool="yosys",
                        tool_version=tool_version, tool_available=False, netlist_sha256=netlist_sha256,
                        confidence="low", limitations=[reason])


def _read_netlist(value: Any) -> tuple[Mapping[str, Any] | None, str | None]:
    if isinstance(value, Mapping):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return value, hashlib.sha256(raw.encode()).hexdigest()
    if isinstance(value, (str, os.PathLike)):
        if isinstance(value, os.PathLike):
            text = Path(value).read_text()
        elif str(value).lstrip().startswith("{"):
            text = str(value)
        else:
            candidate = Path(str(value))
            text = candidate.read_text() if candidate.exists() else str(value)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None, None
        if isinstance(parsed, Mapping):
            return parsed, hashlib.sha256(text.encode()).hexdigest()
    return None, None


def _yosys_version(yosys_path: str) -> str | None:
    try:
        proc = subprocess.run([yosys_path, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0][:240] if proc.returncode == 0 and text else None


def _yosys_netlist(verilog_code: str, top_module: str, yosys_path: str) -> tuple[Mapping[str, Any] | None, str | None, str | None]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", top_module):
        raise ValueError("top_module must be a portable Verilog identifier")
    with tempfile.TemporaryDirectory(prefix="cdc-rdc-") as directory:
        source = Path(directory) / f"{top_module}.v"
        output = Path(directory) / f"{top_module}.json"
        source.write_text(verilog_code, encoding="utf-8")
        script = (f"read_verilog -sv {shlex.quote(str(source))}; "
                  f"hierarchy -top {top_module}; proc; opt; "
                  f"write_json {shlex.quote(str(output))}")
        proc = subprocess.run([yosys_path, "-q", "-p", script],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not output.exists():
            return None, None, (proc.stderr or proc.stdout or "Yosys did not produce JSON")[-1200:]
        text = output.read_text(encoding="utf-8")
        return json.loads(text), hashlib.sha256(text.encode()).hexdigest(), None


def analyze_cdc_rdc(netlist: Mapping[str, Any] | str | os.PathLike[str] | None = None, *,
                    verilog_code: str | None = None, top_module: str = "top",
                    yosys_path: str | None = None,
                    waivers: Iterable[CdcWaiver | Mapping[str, Any]] | None = None,
                    persist_dir: str | os.PathLike[str] | None = None) -> CdcRdcReport:
    """Analyze a Yosys JSON netlist, or lower RTL through Yosys first.

    If RTL is supplied and Yosys is unavailable/fails, the returned report is
    explicitly ``inconclusive``.  A supplied JSON netlist remains analyzable
    without a local Yosys executable, which makes fixtures and locked-down CI
    reproducible.
    """
    parsed, digest = _read_netlist(netlist)
    if parsed is None:
        if verilog_code is None and isinstance(netlist, str):
            try:
                is_path = Path(netlist).exists()
            except OSError:
                is_path = False
            if not is_path:
                verilog_code = netlist
        if verilog_code is None:
            report = _inconclusive(top_module, "no Yosys JSON netlist or Verilog source was supplied")
        else:
            executable = yosys_path or shutil.which("yosys")
            if not executable:
                report = _inconclusive(top_module, "Yosys is not installed; structural CDC/RDC is inconclusive")
            else:
                try:
                    parsed, digest, error = _yosys_netlist(verilog_code, top_module, executable)
                    if parsed is None:
                        report = _inconclusive(top_module, f"Yosys failed to elaborate RTL: {error}", netlist_sha256=digest)
                    else:
                        report = _analyse_netlist(parsed, top_module, tool="yosys", tool_version=_yosys_version(executable),
                                                  netlist_sha256=digest, waivers=waivers)
                except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
                    report = _inconclusive(top_module, f"Yosys invocation failed: {exc}", netlist_sha256=digest)
    else:
        report = _analyse_netlist(parsed, top_module, netlist_sha256=digest, waivers=waivers)
    if persist_dir is not None:
        CdcRdcStore(persist_dir).save(report)
    return report


# Names used by callers that prefer the shorter analyzer terminology.
run_cdc_rdc = analyze_cdc_rdc
analyze_netlist = _analyse_netlist

