"""Typed, auditable timing and implementation constraints.

The constraint model is deliberately declarative.  It describes the contract
that a tool run must consume; it does not execute an SDC/XDC command and does
not infer constraints from RTL.  ``ConstraintSet.to_sdc`` is provided for
adapters that explicitly opt into OpenSTA-compatible rendering.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "constraints/v1"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
# A selector is still data, never an arbitrary Tcl/SDC expression.  Wildcards
# and hierarchy are useful for expert constraints, while command delimiters
# and whitespace are rejected to keep rendering safe.
_SELECTOR = re.compile(r"^[A-Za-z0-9_.$/*?\[\]-]+$")
_TEXT = re.compile(r"^[^\r\n]+$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True,
                              populate_by_name=True)


def _finite(value: float, label: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def _selector(value: str, label: str = "selector") -> str:
    value = value.strip()
    if not value or not _SELECTOR.fullmatch(value):
        raise ValueError(f"{label} must be a safe port/pin selector")
    return value


class TargetConstraint(_Model):
    """The process/device/corner contract used to interpret PPA results."""

    process: str
    device: str
    library: str | None = None
    corner: str = "typical"
    voltage_v: float | None = Field(default=None, gt=0)
    temperature_c: float | None = None

    @field_validator("process", "device", "corner", "library")
    @classmethod
    def valid_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or not _TEXT.fullmatch(value):
            raise ValueError("target constraint text must not be blank or multiline")
        return value

    @field_validator("voltage_v", "temperature_c")
    @classmethod
    def finite_value(cls, value: float | None, info) -> float | None:
        return _finite(value, info.field_name) if value is not None else value


class ClockConstraint(_Model):
    """A primary clock definition in nanoseconds."""

    name: str
    port: str | None = None
    period_ns: float = Field(gt=0)
    waveform_rise_ns: float = 0.0
    waveform_fall_ns: float | None = None
    virtual: bool = False

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        value = value.strip()
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("clock name must be a portable HDL identifier")
        return value

    @field_validator("port")
    @classmethod
    def valid_port(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _IDENTIFIER.fullmatch(value.strip()):
            raise ValueError("clock port must be a portable HDL identifier")
        return value.strip()

    @field_validator("period_ns", "waveform_rise_ns", "waveform_fall_ns")
    @classmethod
    def finite_time(cls, value: float | None, info) -> float | None:
        if value is not None:
            _finite(value, info.field_name)
        return value

    @model_validator(mode="after")
    def valid_waveform(self) -> "ClockConstraint":
        fall = self.waveform_fall_ns if self.waveform_fall_ns is not None else self.period_ns / 2
        if self.waveform_rise_ns < 0 or fall <= self.waveform_rise_ns or fall > self.period_ns:
            raise ValueError("clock waveform must satisfy 0 <= rise < fall <= period")
        if not self.virtual and self.port is None:
            raise ValueError("non-virtual clock requires a source port")
        return self


class IoDelayConstraint(_Model):
    """Min/max external delay for one input or output port selector."""

    port: str
    direction: Literal["input", "output"]
    clock: str
    max_delay_ns: float = Field(ge=0)
    min_delay_ns: float | None = None

    @field_validator("port")
    @classmethod
    def valid_selector(cls, value: str) -> str:
        return _selector(value, "I/O port")

    @field_validator("clock")
    @classmethod
    def valid_clock(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value.strip()):
            raise ValueError("I/O delay clock must be a clock name")
        return value.strip()

    @field_validator("max_delay_ns", "min_delay_ns")
    @classmethod
    def finite_delay(cls, value: float | None, info) -> float | None:
        return _finite(value, info.field_name) if value is not None else value

    @model_validator(mode="after")
    def ordered_delay(self) -> "IoDelayConstraint":
        if self.min_delay_ns is not None and self.min_delay_ns > self.max_delay_ns:
            raise ValueError("min_delay_ns must be <= max_delay_ns")
        return self


class ClockUncertaintyConstraint(_Model):
    clock: str
    setup_ns: float = Field(default=0.0, ge=0)
    hold_ns: float = Field(default=0.0, ge=0)

    @field_validator("clock")
    @classmethod
    def valid_clock(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value.strip()):
            raise ValueError("clock uncertainty must reference a clock name")
        return value.strip()

    @field_validator("setup_ns", "hold_ns")
    @classmethod
    def finite_uncertainty(cls, value: float, info) -> float:
        return _finite(value, info.field_name)


class InputDriveConstraint(_Model):
    """Library cell used to model an external driver for selected inputs."""

    port: str
    library_cell: str

    @field_validator("port")
    @classmethod
    def valid_port(cls, value: str) -> str:
        return _selector(value, "input-drive port")

    @field_validator("library_cell")
    @classmethod
    def valid_cell(cls, value: str) -> str:
        value = value.strip()
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("input-drive library cell must be a portable identifier")
        return value


class OutputLoadConstraint(_Model):
    """External pin capacitance applied to selected outputs, in picofarads."""

    port: str
    capacitance_pf: float = Field(gt=0)

    @field_validator("port")
    @classmethod
    def valid_port(cls, value: str) -> str:
        return _selector(value, "output-load port")

    @field_validator("capacitance_pf")
    @classmethod
    def finite_capacitance(cls, value: float) -> float:
        return _finite(value, "capacitance_pf")


class CaseAnalysisConstraint(_Model):
    """A reviewed static value for a profile/configuration input.

    Case analysis is intentionally distinct from a timing exception: the
    selected value is part of the elaborated hardware profile and paths are
    still timed through the selected logic cone.
    """

    port: str
    value: int = Field(ge=0, le=1)
    reason: str

    @field_validator("port")
    @classmethod
    def valid_port(cls, value: str) -> str:
        return _selector(value, "case-analysis port")

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        value = value.strip()
        if not value or not _TEXT.fullmatch(value):
            raise ValueError("case-analysis reason must not be blank or multiline")
        return value


class ElectricalLimits(_Model):
    """Reviewed design-rule limits; omitted values remain explicitly unset."""

    max_transition_ns: float | None = Field(default=None, gt=0)
    max_capacitance_pf: float | None = Field(default=None, gt=0)
    max_fanout: float | None = Field(default=None, gt=0)

    @field_validator("max_transition_ns", "max_capacitance_pf", "max_fanout")
    @classmethod
    def finite_limit(cls, value: float | None, info) -> float | None:
        return _finite(value, info.field_name) if value is not None else value


class FalsePathConstraint(_Model):
    """An explicitly scoped path exception; at least one endpoint is required."""

    from_path: str | None = Field(default=None, alias="from")
    to_path: str | None = Field(default=None, alias="to")
    through: str | None = None
    reason: str

    @field_validator("from_path", "to_path", "through")
    @classmethod
    def valid_endpoint(cls, value: str | None, info) -> str | None:
        return _selector(value, info.field_name) if value is not None else value

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        value = value.strip()
        if not value or not _TEXT.fullmatch(value):
            raise ValueError("false-path reason must not be blank or multiline")
        return value

    @model_validator(mode="after")
    def bounded(self) -> "FalsePathConstraint":
        if not any((self.from_path, self.to_path, self.through)):
            raise ValueError("false path must specify from, to, or through")
        return self


class MulticyclePathConstraint(_Model):
    """A scoped setup/hold path exception, with explicit cycle counts."""

    from_path: str | None = Field(default=None, alias="from")
    to_path: str | None = Field(default=None, alias="to")
    setup_cycles: int = Field(ge=1)
    hold_cycles: int | None = Field(default=None, ge=0)
    reason: str

    @field_validator("from_path", "to_path")
    @classmethod
    def valid_endpoint(cls, value: str | None, info) -> str | None:
        return _selector(value, info.field_name) if value is not None else value

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        value = value.strip()
        if not value or not _TEXT.fullmatch(value):
            raise ValueError("multicycle reason must not be blank or multiline")
        return value

    @model_validator(mode="after")
    def bounded(self) -> "MulticyclePathConstraint":
        if not self.from_path and not self.to_path:
            raise ValueError("multicycle path must specify from or to")
        if self.hold_cycles is not None and self.hold_cycles > self.setup_cycles:
            raise ValueError("hold_cycles must be <= setup_cycles")
        return self


class ConstraintSet(_Model):
    """Complete, versioned implementation constraint contract for a design."""

    schema_version: str = SCHEMA_VERSION
    target: TargetConstraint | None = None
    clocks: list[ClockConstraint] = Field(default_factory=list)
    io_delays: list[IoDelayConstraint] = Field(default_factory=list)
    uncertainties: list[ClockUncertaintyConstraint] = Field(default_factory=list)
    input_drives: list[InputDriveConstraint] = Field(default_factory=list)
    output_loads: list[OutputLoadConstraint] = Field(default_factory=list)
    case_analysis: list[CaseAnalysisConstraint] = Field(default_factory=list)
    electrical_limits: ElectricalLimits | None = None
    false_paths: list[FalsePathConstraint] = Field(default_factory=list)
    multicycle_paths: list[MulticyclePathConstraint] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported constraint schema version: {value}")
        return value

    @field_validator("assumptions")
    @classmethod
    def valid_assumptions(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.strip() or not _TEXT.fullmatch(value):
                raise ValueError("constraint assumptions must not be blank or multiline")
        return [value.strip() for value in values]

    @model_validator(mode="after")
    def consistent_references(self) -> "ConstraintSet":
        names = {item.name for item in self.clocks}
        ports = {item.port for item in self.clocks if item.port is not None}
        if len(names) != len(self.clocks):
            raise ValueError("clock names must be unique")
        if len(ports) != sum(item.port is not None for item in self.clocks):
            raise ValueError("clock ports must be unique")
        for item in [*self.io_delays, *self.uncertainties]:
            if item.clock not in names and item.clock not in ports:
                raise ValueError(f"constraint references unknown clock: {item.clock}")
        return self

    def canonical_data(self) -> dict[str, Any]:
        # Storage/hashing uses field names, matching evidence.canonical_json.
        # Aliases (``from``/``to``) remain accepted at the JSON input boundary.
        return self.model_dump(mode="json", by_alias=False, exclude_none=False)

    def content_sha256(self) -> str:
        content = json.dumps(self.canonical_data(), sort_keys=True,
                             ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _query(value: str, kind: str = "ports") -> str:
        return f"[get_{kind} {{{value}}}]"

    def to_sdc(self) -> str:
        """Render a constrained subset of SDC for an explicitly chosen adapter."""
        lines = [f"# generated from {SCHEMA_VERSION}; constraints are declarative evidence"]
        for clock in self.clocks:
            fall = clock.waveform_fall_ns if clock.waveform_fall_ns is not None else clock.period_ns / 2
            # A virtual clock has no source object.  ``-add`` is not a
            # virtual-clock switch; emitting a port query would make the
            # constraint target a real clock pin and change its meaning.
            target = "" if clock.virtual else f" {self._query(clock.port)}"
            lines.append(f"create_clock -name {clock.name} -period {clock.period_ns:g}"
                         f" -waveform {{ {clock.waveform_rise_ns:g} {fall:g} }}{target}")
        for item in self.uncertainties:
            clock = f"[get_clocks {{{item.clock}}}]"
            if item.setup_ns:
                lines.append(f"set_clock_uncertainty -setup {item.setup_ns:g} {clock}")
            if item.hold_ns:
                lines.append(f"set_clock_uncertainty -hold {item.hold_ns:g} {clock}")
        for item in self.io_delays:
            flag = "input" if item.direction == "input" else "output"
            query = self._query(item.port)
            clock = f"[get_clocks {{{item.clock}}}]"
            if item.min_delay_ns is not None:
                lines.append(f"set_{flag}_delay -min {item.min_delay_ns:g} -clock {clock} {query}")
            lines.append(f"set_{flag}_delay -max {item.max_delay_ns:g} -clock {clock} {query}")
        for item in self.input_drives:
            lines.append(
                f"set_driving_cell -lib_cell {item.library_cell} {self._query(item.port)}"
            )
        for item in self.output_loads:
            lines.append(f"set_load -pin_load {item.capacitance_pf:g} {self._query(item.port)}")
        for item in self.case_analysis:
            lines.append(f"set_case_analysis {item.value} {self._query(item.port)}")
        if self.electrical_limits is not None:
            limits = self.electrical_limits
            if limits.max_transition_ns is not None:
                lines.append(f"set_max_transition {limits.max_transition_ns:g} [current_design]")
            if limits.max_capacitance_pf is not None:
                lines.append(f"set_max_capacitance {limits.max_capacitance_pf:g} [current_design]")
            if limits.max_fanout is not None:
                lines.append(f"set_max_fanout {limits.max_fanout:g} [current_design]")
        for item in self.false_paths:
            parts = ["set_false_path"]
            if item.from_path:
                parts += ["-from", self._query(item.from_path)]
            if item.to_path:
                parts += ["-to", self._query(item.to_path)]
            if item.through:
                parts += ["-through", self._query(item.through, "pins")]
            lines.append(" ".join(parts))
        for item in self.multicycle_paths:
            endpoints = []
            if item.from_path:
                endpoints += ["-from", self._query(item.from_path)]
            if item.to_path:
                endpoints += ["-to", self._query(item.to_path)]
            lines.append(" ".join(["set_multicycle_path", str(item.setup_cycles), "-setup", *endpoints]))
            if item.hold_cycles is not None:
                lines.append(" ".join(["set_multicycle_path", str(item.hold_cycles), "-hold", *endpoints]))
        return "\n".join(lines) + "\n"


    def to_xdc(self) -> str:
        """Render the portable subset in Xilinx XDC syntax.

        The constrained subset is intentionally SDC-compatible; keeping one
        canonical renderer prevents semantic drift between ASIC and FPGA
        evidence.
        """
        return self.to_sdc()

    def render(self, format: Literal["sdc", "xdc"] = "sdc") -> str:
        """Render constraints for an explicitly selected adapter format."""
        if format == "sdc":
            return self.to_sdc()
        if format == "xdc":
            return self.to_xdc()
        raise ValueError(f"unsupported constraint format: {format}")


__all__ = [
    "SCHEMA_VERSION", "ClockConstraint", "ClockUncertaintyConstraint",
    "ConstraintSet", "ElectricalLimits", "FalsePathConstraint",
    "CaseAnalysisConstraint", "InputDriveConstraint", "IoDelayConstraint",
    "MulticyclePathConstraint",
    "OutputLoadConstraint", "TargetConstraint",
]
