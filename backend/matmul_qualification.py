"""Offline, reproducible qualification contract for the ``matmul3`` benchmark.

This module defines the frozen benchmark contract, an independent cycle model,
deterministic vectors, and the bounded evidence reducer. The executable RTL
and optional Sky130 stages live in ``matmul_rtl_runner.py`` so preflight and
reference tests remain free of model, simulator, and EDA side effects.

The benchmark is ASIC engineering evidence for the Sky130 standard-cell flow;
it is not Tiny Tapeout acceptance or foundry signoff.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import inspect
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence


QUALIFICATION_SCHEMA = "matmul3-qualification/v1"
VECTOR_SET_ID = "matmul3-v1-fixed-seeds-3-17-29"
MATMUL_N = 4
INT8_BITS = 8
INT32_BITS = 32
TARGET_FMAX_MHZ = 50.0
TARGET_PERIOD_NS = 20.0
DEFAULT_VECTOR_COUNT = 40
DEFAULT_SEED = 3
GOLDEN_MODEL_ID = "golden.matmul4x4"
GOLDEN_MODEL_VERSION = "1.0.0"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _signed(value: int, bits: int) -> int:
    value = int(value) & ((1 << bits) - 1)
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


def _validate_matrix(values: Sequence[int], bits: int, name: str) -> tuple[int, ...]:
    expected = MATMUL_N * MATMUL_N
    if len(values) != expected:
        raise ValueError(f"{name} must contain {expected} row-major elements")
    low, high = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    result = tuple(int(value) for value in values)
    if any(value < low or value > high for value in result):
        raise ValueError(f"{name} values must be signed {bits}-bit integers")
    return result


@dataclass(frozen=True)
class InterfacePort:
    name: str
    direction: str
    width: int
    description: str


@dataclass(frozen=True)
class MatmulQualificationSpec:
    """The immutable contract shared by the reference and RTL adapters."""

    schema_version: str = QUALIFICATION_SCHEMA
    benchmark: str = "matmul3"
    top_module: str = "systolic_mm_4x4"
    technology: str = "sky130"
    target_flow: str = "skywater130_demo"
    dimension: int = MATMUL_N
    input_encoding: str = "signed two's-complement int8"
    output_encoding: str = "signed two's-complement int32"
    arithmetic: str = "C[i,j] = sum(A[i,k] * B[k,j]) for k=0..3; no saturation"
    latency_cycles: int = 10
    initiation_interval_cycles: int = 10
    target_fmax_mhz: float = TARGET_FMAX_MHZ
    target_period_ns: float = TARGET_PERIOD_NS
    reset: str = "asynchronous active-low rst_n; clears in-flight transaction"
    interfaces: tuple[InterfacePort, ...] = field(default_factory=lambda: (
        InterfacePort("clk", "input", 1, "clock"),
        InterfacePort("rst_n", "input", 1, "asynchronous active-low reset"),
        InterfacePort("a_write", "input", 1, "write one A matrix element"),
        InterfacePort("b_write", "input", 1, "write one B matrix element"),
        InterfacePort("write_index", "input", 4, "row-major element index 0..15"),
        InterfacePort("a_data", "input", 8, "signed int8 A element"),
        InterfacePort("b_data", "input", 8, "signed int8 B element"),
        InterfacePort("start", "input", 1, "start after both matrices are loaded"),
        InterfacePort("busy", "output", 1, "compute in progress"),
        InterfacePort("done", "output", 1, "one-cycle completion pulse"),
        InterfacePort("read_index", "input", 4, "row-major result index 0..15"),
        InterfacePort("c_data", "output", 32, "signed int32 result element; combinational read"),
    ))

    def __post_init__(self) -> None:
        if self.schema_version != QUALIFICATION_SCHEMA:
            raise ValueError(f"unsupported qualification schema: {self.schema_version}")
        if self.dimension != MATMUL_N:
            raise ValueError("matmul3 is frozen at dimension 4")
        if self.latency_cycles < 1 or self.initiation_interval_cycles < 1:
            raise ValueError("latency and initiation interval must be positive")
        if not math.isclose(self.target_period_ns, 1000.0 / self.target_fmax_mhz,
                            rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("target frequency and period are inconsistent")

    def canonical(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["interfaces"] = [asdict(port) for port in self.interfaces]
        return payload

    @property
    def digest(self) -> str:
        return _sha256(self.canonical())


MATMUL_SPEC = MatmulQualificationSpec()


def matmul_reference(a: Sequence[int], b: Sequence[int]) -> tuple[int, ...]:
    """Human-authored mathematical reference, independent of generated RTL."""

    aa = _validate_matrix(a, INT8_BITS, "a")
    bb = _validate_matrix(b, INT8_BITS, "b")
    result: list[int] = []
    for row in range(MATMUL_N):
        for col in range(MATMUL_N):
            result.append(sum(aa[row * MATMUL_N + k] * bb[k * MATMUL_N + col]
                              for k in range(MATMUL_N)))
    return tuple(result)


def golden_model_provenance() -> dict[str, Any]:
    source = inspect.getsource(matmul_reference)
    return {
        "model_id": GOLDEN_MODEL_ID,
        "version": GOLDEN_MODEL_VERSION,
        "source": "backend/matmul_qualification.py:matmul_reference",
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "generated_by": "human",
        "independent_of_rtl": True,
    }


@dataclass(frozen=True)
class MatmulVector:
    vector_id: str
    a: tuple[int, ...]
    b: tuple[int, ...]
    expected: tuple[int, ...]

    def canonical(self) -> dict[str, Any]:
        return {"vector_id": self.vector_id, "a": list(self.a),
                "b": list(self.b), "expected": list(self.expected)}


def _directed_inputs() -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    zero = (0,) * 16
    one = (1,) * 16
    identity = tuple(1 if row == col else 0 for row in range(4) for col in range(4))
    neg_identity = tuple(-1 if row == col else 0 for row in range(4) for col in range(4))
    extrema = tuple(127 if index % 2 == 0 else -128 for index in range(16))
    alternating = tuple(-3 if index % 2 == 0 else 5 for index in range(16))
    cases = [
        (zero, zero), (zero, alternating), (alternating, zero),
        (identity, one), (one, identity), (identity, neg_identity),
        (tuple([-1] * 16), tuple([-1] * 16)),
        (tuple([-128] * 16), tuple([-128] * 16)),
        (extrema, extrema), (alternating, tuple(reversed(alternating))),
        (tuple(range(-8, 8)), tuple(range(8, -8, -1))),
        (tuple([127] * 16), tuple([-128] * 16)),
    ]
    # Basis pairs isolate every C[row,col] position through k=0.
    for row in range(4):
        for col in range(4):
            a = [0] * 16; b = [0] * 16
            a[row * 4] = -128 if (row + col) % 2 else 127
            b[col] = 127 if (row + col) % 2 else -128
            cases.append((tuple(a), tuple(b)))
    return cases


def qualification_vectors(*, seed: int = DEFAULT_SEED,
                           count: int = DEFAULT_VECTOR_COUNT) -> tuple[MatmulVector, ...]:
    """Return frozen directed corners followed by deterministic random vectors."""

    if count <= 0:
        raise ValueError("count must be positive")
    pairs = _directed_inputs()
    # Three frozen streams prevent one lucky random seed from defining the gate.
    rng_streams = [random.Random(value) for value in (seed, 17, 29)]
    for index in range(max(0, count - len(pairs))):
        rng = rng_streams[index % len(rng_streams)]
        pairs.append((
            tuple(rng.randint(-128, 127) for _ in range(16)),
            tuple(rng.randint(-128, 127) for _ in range(16)),
        ))
    return tuple(
        MatmulVector(f"{VECTOR_SET_ID}-{index:04d}", a, b, matmul_reference(a, b))
        for index, (a, b) in enumerate(pairs[:count])
    )


def vector_set_digest(vectors: Iterable[MatmulVector]) -> str:
    return _sha256([vector.canonical() for vector in vectors])


@dataclass(frozen=True)
class CycleSample:
    cycle: int
    busy: bool
    done: bool
    c_data: int
    start_accepted: bool = False


class MatmulGoldenModel:
    """Cycle model for the frozen indexed-write/start/done contract.

    Matrix elements are written while idle.  A rising edge accepting ``start``
    captures both matrices and schedules ``done`` exactly ``latency_cycles``
    cycles later.  ``c_data`` is a combinational indexed read of the retained
    result, including during and after ``done``.
    """

    def __init__(self, spec: MatmulQualificationSpec = MATMUL_SPEC):
        self.spec = spec
        self.reset()

    def reset(self) -> None:
        self._cycle = 0
        self._a = [0] * 16
        self._b = [0] * 16
        self._result = [0] * 16
        self._busy = False
        self._due_cycle: int | None = None

    def step(self, *, rst_n: bool = True, a_write: bool = False,
             b_write: bool = False, write_index: int = 0, a_data: int = 0,
             b_data: int = 0, start: bool = False, read_index: int = 0) -> CycleSample:
        cycle = self._cycle
        if not rst_n:
            self.reset()
            self._cycle = cycle + 1
            return CycleSample(cycle, False, False, 0, False)
        if not 0 <= int(write_index) < 16 or not 0 <= int(read_index) < 16:
            raise ValueError("write_index/read_index must be in 0..15")
        idle_before = not self._busy
        if idle_before and a_write:
            self._a[int(write_index)] = _signed(a_data, 8)
        if idle_before and b_write:
            self._b[int(write_index)] = _signed(b_data, 8)
        done = self._busy and self._due_cycle == cycle
        if done:
            self._busy = False
            self._due_cycle = None
        accepted = bool(start and idle_before)
        if accepted:
            self._result = list(matmul_reference(self._a, self._b))
            self._busy = True
            self._due_cycle = cycle + self.spec.latency_cycles
        sample = CycleSample(cycle, self._busy, done,
                             self._result[int(read_index)], accepted)
        self._cycle = cycle + 1
        return sample

    def load_and_run(self, vector: MatmulVector, *, read_index: int = 0) -> tuple[CycleSample, ...]:
        samples: list[CycleSample] = []
        for index, (a_value, b_value) in enumerate(zip(vector.a, vector.b)):
            samples.append(self.step(a_write=True, b_write=True,
                                     write_index=index, a_data=a_value,
                                     b_data=b_value, read_index=read_index))
        started = self.step(start=True, read_index=read_index)
        samples.append(started)
        if not started.start_accepted:
            raise AssertionError("golden model failed to accept start while idle")
        while True:
            sample = self.step(read_index=read_index)
            samples.append(sample)
            if sample.done:
                if sample.cycle != started.cycle + self.spec.latency_cycles:
                    raise AssertionError("golden latency contract violated")
                break
        return tuple(samples)


class QualificationStatus(StrEnum):
    PREFLIGHT = "preflight"
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class QualificationLimits:
    max_model_rounds: int = 6
    max_rtl_revisions: int = 2
    max_screen_invocations: int = 3
    max_full_implementation_runs: int = 1
    max_tokens: int = 120_000
    max_tool_calls: int = 12
    max_wall_seconds: float = 1_800.0

    def __post_init__(self) -> None:
        if any(value < 0 for value in (self.max_model_rounds, self.max_rtl_revisions,
                                       self.max_screen_invocations,
                                       self.max_full_implementation_runs,
                                       self.max_tokens, self.max_tool_calls)):
            raise ValueError("qualification limits cannot be negative")
        if self.max_wall_seconds <= 0 or not math.isfinite(self.max_wall_seconds):
            raise ValueError("max_wall_seconds must be finite and positive")

    def violations(self, usage: Mapping[str, Any]) -> tuple[str, ...]:
        checks = (
            ("model rounds", usage.get("model_rounds", 0), self.max_model_rounds),
            ("RTL revisions", usage.get("rtl_revisions", 0), self.max_rtl_revisions),
            ("cheap screens", usage.get("screen_invocations", 0), self.max_screen_invocations),
            ("full implementation runs", usage.get("full_implementation_runs", 0), self.max_full_implementation_runs),
            ("tokens", usage.get("tokens", 0), self.max_tokens),
            ("tool calls", usage.get("tool_calls", 0), self.max_tool_calls),
            ("wall time", usage.get("wall_seconds", 0.0), self.max_wall_seconds),
        )
        return tuple(f"{name} budget exceeded ({actual} > {limit})"
                     for name, actual, limit in checks if actual > limit)


@dataclass(frozen=True)
class CandidateEvidence:
    revision: int
    simulation_passed: bool
    regression_passed: bool
    fmax_mhz: float | None = None
    slack_ns: float | None = None
    critical_path: str | None = None
    area: float | None = None


@dataclass(frozen=True)
class QualificationReport:
    status: QualificationStatus
    spec_digest: str
    vector_set_digest: str
    vectors: int
    limits: QualificationLimits
    usage: Mapping[str, Any]
    budget_violations: tuple[str, ...] = ()
    best_revision: int | None = None
    best_fmax_mhz: float | None = None
    critical_path: str | None = None
    remaining_gap_mhz: float | None = None
    reason: str = ""
    bounded_failure: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["limits"] = asdict(self.limits)
        payload["budget_violations"] = list(self.budget_violations)
        return payload


def preflight(*, spec: MatmulQualificationSpec = MATMUL_SPEC,
              limits: QualificationLimits = QualificationLimits(),
              seed: int = DEFAULT_SEED,
              count: int = DEFAULT_VECTOR_COUNT) -> QualificationReport:
    """Validate the frozen qualification inputs without LLM/EDA/tool calls."""

    vectors = qualification_vectors(seed=seed, count=count)
    return QualificationReport(
        QualificationStatus.PREFLIGHT, spec.digest, vector_set_digest(vectors),
        len(vectors), limits, {}, reason=(
            "preflight passed: frozen contract, independent reference, "
            "cycle latency, and deterministic vectors validated; no model or EDA run"
        ),
    )


def evaluate_run(*, candidates: Sequence[CandidateEvidence], usage: Mapping[str, Any],
                 final_implementation_passed: bool,
                 spec: MatmulQualificationSpec = MATMUL_SPEC,
                 limits: QualificationLimits = QualificationLimits(),
                 seed: int = DEFAULT_SEED,
                 count: int = DEFAULT_VECTOR_COUNT) -> QualificationReport:
    """Reduce recorded run evidence to a conservative pass/fail report."""

    vectors = qualification_vectors(seed=seed, count=count)
    violations = limits.violations(usage)
    valid = [candidate for candidate in candidates
             if candidate.simulation_passed and candidate.regression_passed
             and candidate.fmax_mhz is not None]
    # Preserve the best verified implementation, regardless of the order in
    # which later candidates arrive.  A lower-Fmax candidate can never become
    # the baseline merely because it was measured last.
    best = max(valid, key=lambda item: (item.fmax_mhz, -(item.area or float("inf"))),
               default=None)
    if best is None:
        return QualificationReport(
            QualificationStatus.INCONCLUSIVE if not violations else QualificationStatus.FAILED,
            spec.digest, vector_set_digest(vectors), len(vectors), limits, dict(usage),
            violations, reason="no simulation- and regression-passing timing evidence",
            bounded_failure="The run produced no verified candidate with measured Fmax; preserve the last passing RTL and rerun from the recorded checkpoint.",
        )
    gap = max(0.0, spec.target_fmax_mhz - float(best.fmax_mhz))
    if violations:
        status = QualificationStatus.FAILED
        reason = "qualification budget exceeded"
    elif not final_implementation_passed:
        status = QualificationStatus.INCONCLUSIVE
        reason = "no passing final implementation evidence was recorded"
    elif float(best.fmax_mhz) < spec.target_fmax_mhz:
        status = QualificationStatus.FAILED
        reason = f"best verified Fmax {best.fmax_mhz:.3f} MHz is below {spec.target_fmax_mhz:.3f} MHz"
    else:
        status = QualificationStatus.PASSED
        reason = f"revision {best.revision} meets the {spec.target_fmax_mhz:.3f} MHz qualification target"
    bounded = None if status is QualificationStatus.PASSED else (
        f"Retained best revision {best.revision} at {best.fmax_mhz:.3f} MHz; "
        f"remaining gap {gap:.3f} MHz. Critical path: {best.critical_path or 'not reported'}."
    )
    return QualificationReport(
        status, spec.digest, vector_set_digest(vectors), len(vectors), limits,
        dict(usage), violations, best.revision, float(best.fmax_mhz),
        best.critical_path, gap, reason, bounded,
    )

