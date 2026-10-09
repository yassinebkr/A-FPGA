"""Board-neutral, validated FPGA target profiles.

Profiles are declarative inputs for a future build/program/HIL adapter.  This
module deliberately does not execute a toolchain or encode a particular board:
it only validates the physical target contract and gives callers a stable,
content-addressed representation to persist with verification evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "fpga-profile/v1"
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _canonical(value: Any) -> Any:
    if isinstance(value, BaseModel):
        try:
            return value.model_dump(mode="json")
        except AttributeError:  # pragma: no cover - pydantic v1 fallback
            return json.loads(value.json())
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def canonical_json(value: Any, *, omit_hash: bool = False) -> str:
    """Encode a profile deterministically for hashing or persistence."""
    data = _canonical(value)
    if omit_hash and isinstance(data, dict):
        data.pop("profile_sha256", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(profile: "FpgaProfile") -> str:
    return hashlib.sha256(canonical_json(profile, omit_hash=True).encode("utf-8")).hexdigest()


def _safe_id(value: str, field_name: str) -> str:
    value = value.strip()
    if not _ID.fullmatch(value):
        raise ValueError(f"{field_name} must be a portable identifier")
    return value


class FpgaToolchain(_ProfileModel):
    """Toolchain identity, without executable paths or invocation semantics."""

    name: str
    version: str
    backend: str | None = None
    constraint_format: Literal["xdc", "sdc", "pcf", "qsf", "generic"] = "generic"

    @field_validator("name", "version", "backend")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("toolchain fields must not be blank")
        return value.strip() if value is not None else value


class FpgaClock(_ProfileModel):
    port: str
    frequency_mhz: float = Field(gt=0)
    duty_cycle_percent: float = Field(default=50.0, gt=0, lt=100)
    tolerance_percent: float = Field(default=5.0, ge=0, lt=100)

    @field_validator("port")
    @classmethod
    def valid_port(cls, value: str) -> str:
        return _safe_id(value, "clock port")

    @field_validator("frequency_mhz", "duty_cycle_percent", "tolerance_percent")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("clock values must be finite")
        return value


class FpgaReset(_ProfileModel):
    port: str
    polarity: Literal["active_high", "active_low"] = "active_low"
    synchronous: bool = False

    @field_validator("port")
    @classmethod
    def valid_port(cls, value: str) -> str:
        return _safe_id(value, "reset port")


class FpgaIo(_ProfileModel):
    """One logical HDL port and its board-level mapping."""

    port: str
    direction: Literal["input", "output", "inout"]
    pin: str
    width: int = Field(default=1, ge=1)
    io_standard: str | None = None
    pull: Literal["none", "up", "down"] = "none"
    drive_ma: float | None = Field(default=None, gt=0)
    slew: Literal["slow", "medium", "fast"] | None = None

    @field_validator("port", "pin")
    @classmethod
    def nonblank_mapping(cls, value: str, info) -> str:
        value = value.strip()
        if not value:
            raise ValueError(f"{info.field_name} must not be blank")
        return value

    @field_validator("io_standard")
    @classmethod
    def nonblank_standard(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("io_standard must not be blank")
        return value.strip() if value is not None else value

    @field_validator("drive_ma")
    @classmethod
    def finite_drive(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("drive_ma must be finite")
        return value


class FpgaProgramming(_ProfileModel):
    """How a future adapter reaches the board, not a command to execute."""

    transport: Literal["jtag", "usb", "serial", "ethernet", "file"]
    programmer: str
    bitstream_format: Literal["bit", "bin", "sof", "svf", "hex", "raw"] = "bit"
    verify_after_program: bool = True
    persistent: bool = False

    @field_validator("programmer")
    @classmethod
    def valid_programmer(cls, value: str) -> str:
        return _safe_id(value, "programmer")


class FpgaProfile(_ProfileModel):
    """Complete board-neutral target description for FPGA/HIL work."""

    schema_version: str = SCHEMA_VERSION
    profile_id: str
    profile_version: str = "1.0.0"
    board_id: str
    vendor: str
    family: str
    part: str
    toolchain: FpgaToolchain
    clock: FpgaClock
    reset: FpgaReset | None = None
    io: list[FpgaIo] = Field(default_factory=list)
    programming: FpgaProgramming
    timeout_s: float = Field(default=30.0, gt=0)
    profile_sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported FPGA profile schema: {value}")
        return value

    @field_validator("profile_id", "board_id", "vendor", "family", "part", "profile_version")
    @classmethod
    def nonblank_identity(cls, value: str, info) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be blank")
        return value.strip()

    @field_validator("timeout_s")
    @classmethod
    def finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timeout_s must be finite")
        return value

    @field_validator("profile_sha256")
    @classmethod
    def valid_digest(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("profile_sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def unique_ports(self) -> "FpgaProfile":
        ports = [item.port for item in self.io]
        if len(ports) != len(set(ports)):
            raise ValueError("FPGA IO port mappings must be unique")
        pins = [item.pin for item in self.io]
        if len(pins) != len(set(pins)):
            raise ValueError("FPGA IO pin mappings must be unique")
        if self.reset is not None and self.reset.port == self.clock.port:
            raise ValueError("clock and reset ports must be distinct")
        return self

    def content_sha256(self) -> str:
        """Return the canonical digest, excluding the stored digest field."""
        return _digest(self)

    def with_hash(self) -> "FpgaProfile":
        """Return an equivalent profile carrying its canonical content hash."""
        return self.model_copy(update={"profile_sha256": self.content_sha256()})

    def hash_matches(self) -> bool:
        """Whether ``profile_sha256`` is present and matches this profile."""
        return self.profile_sha256 is not None and self.profile_sha256 == self.content_sha256()

