"""Machine-readable architecture contracts for architecture-first RTL generation.

The contract is deliberately small and technology-neutral.  It describes the
microarchitecture assumptions that must be agreed before generated RTL is
accepted by the EDA loop; it is not a timing report or a signoff record.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = "architecture/v1"
_TEXT = re.compile(r"^[^\r\n]+$")


class ArchitectureContract(BaseModel):
    """Approved implementation intent for one generated design.

    ``clock_target_mhz`` and ``clock_period_ns`` are both kept because the
    former is what users specify and the latter is what STA consumes.  Both
    are required and validated as the same physical target.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: str = SCHEMA_VERSION
    target_type: Literal["fpga", "asic"]
    top_module: str = Field(min_length=1)
    technology: str = Field(min_length=1)
    clock_target_mhz: float = Field(gt=0)
    clock_period_ns: float = Field(gt=0)
    latency_cycles: int = Field(ge=1)
    throughput_per_cycle: float = Field(gt=0)
    pipeline_stages: int = Field(ge=0)
    multiplier_strategy: Literal[
        "inferred", "dsp", "fabric", "macro", "none"
    ]
    memory_strategy: Literal[
        "registers", "bram", "sram", "distributed_ram", "external", "none"
    ]
    rationale: str | None = None

    @model_validator(mode="after")
    def validate_contract(self) -> "ArchitectureContract":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported architecture schema version: {self.schema_version}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", self.top_module):
            raise ValueError("top_module must be a portable HDL identifier")
        for field in ("top_module", "technology", "rationale"):
            value = getattr(self, field)
            if value is not None and (not value.strip() or not _TEXT.fullmatch(value)):
                raise ValueError(f"{field} must be a non-empty single-line string")
        if not math.isfinite(self.clock_target_mhz) or not math.isfinite(self.clock_period_ns):
            raise ValueError("clock target must be finite")
        expected_period = 1000.0 / self.clock_target_mhz
        # A decimal frequency/period pair may have normal display rounding,
        # but must still describe the same target to the STA adapter.
        if not math.isclose(self.clock_period_ns, expected_period, rel_tol=2e-3, abs_tol=1e-3):
            raise ValueError(
                "clock_period_ns must equal 1000 / clock_target_mhz "
                f"(expected approximately {expected_period:g} ns)"
            )
        if self.pipeline_stages > self.latency_cycles:
            raise ValueError("pipeline_stages cannot exceed latency_cycles")
        return self

    def canonical(self) -> dict[str, Any]:
        """Return stable JSON-compatible contract data."""
        return self.model_dump(mode="json", exclude_none=True)


def contract_hash(contract: ArchitectureContract | Mapping[str, Any]) -> str:
    """Return the content hash used to bind RTL work to this contract."""
    if not isinstance(contract, ArchitectureContract):
        contract = ArchitectureContract.model_validate(contract)
    payload = json.dumps(contract.canonical(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def define_architecture(**payload: Any) -> str:
    """Validate and approve a contract for the agent tool loop.

    The agent layer records the returned contract in per-session state.  This
    function remains stateless so validation is deterministic and safe to use
    in tests or from another frontend later.
    """
    try:
        contract = ArchitectureContract.model_validate(payload)
    except Exception as exc:
        return json.dumps({"ok": False, "approved": False, "error": str(exc)})
    return json.dumps({
        "ok": True,
        "approved": True,
        "contract": contract.canonical(),
        "contract_sha256": contract_hash(contract),
    }, sort_keys=True)


def validate_rtl_against_contract(
    contract: ArchitectureContract | Mapping[str, Any],
    rtl: str,
    *,
    top_module: str | None = None,
    tool: str = "",
    tool_args: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return explicit contradictions between RTL/tool intent and contract.

    This is intentionally conservative: it rejects only contradictions that
    can be established from source or tool arguments.  It does not pretend to
    prove latency, throughput, DSP inference, or pipeline depth statically;
    those remain simulation/synthesis obligations.
    """
    if not isinstance(contract, ArchitectureContract):
        contract = ArchitectureContract.model_validate(contract)
    rtl = str(rtl or "")
    args = tool_args or {}
    errors: list[str] = []

    expected_top = contract.top_module
    if top_module and top_module != expected_top:
        errors.append(f"top_module '{top_module}' contradicts contract top_module '{expected_top}'")
    module_re = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b")
    modules = module_re.findall(rtl)
    if modules and expected_top not in modules:
        errors.append(f"contract top_module '{expected_top}' is not declared by the RTL")

    if tool in {"run_silicon_compiler", "find_fmax"}:
        period = args.get("clock_period_ns")
        if period is not None:
            try:
                if not math.isclose(float(period), contract.clock_period_ns,
                                    rel_tol=2e-3, abs_tol=1e-3):
                    errors.append(
                        f"clock_period_ns {float(period):g} ns contradicts the architecture "
                        f"contract target {contract.clock_period_ns:g} ns"
                    )
            except (TypeError, ValueError):
                errors.append("clock_period_ns is not a finite number")
        target = str(args.get("target") or "").lower()
        if contract.target_type == "asic" and any(
            marker in target for marker in ("ice40", "ecp5", "xilinx", "xc7", "artix", "cyclone", "gowin")
        ):
            errors.append("FPGA synthesis target contradicts an ASIC architecture contract")
        if contract.target_type == "fpga" and any(
            marker in target for marker in ("skywater", "sky130", "freepdk", "asap7")
        ):
            errors.append("ASIC/PDK synthesis target contradicts an FPGA architecture contract")

    # Memory/DSP strategy is architectural intent.  It is deliberately not
    # inferred from Verilog syntax here: register banks may be represented by
    # unpacked arrays and technology mapping is a synthesis obligation.

    if contract.pipeline_stages > 0 and not re.search(
        r"(?i)\balways\s*(?:_ff\s*)?@\s*\(\s*posedge\b|\balways_ff\b", rtl
    ):
        errors.append("contract requires pipeline stages but RTL has no clocked process")

    return errors

