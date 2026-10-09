"""Independent golden models and deterministic differential regression support.

This module is intentionally independent from generated Verilog and from the
testbench generator.  A reference model is a small, reviewable Python
function.  The regression runner can be given any DUT adapter (a Python
function, an RTL simulator wrapper, or a hardware harness) with the same
vector contract.

The persisted corpus stores inputs and the expected output produced by the
versioned reference model.  A changed reference therefore cannot silently
change the meaning of an old regression: the runner reports a reference
failure before invoking the DUT.
"""

from __future__ import annotations

import hashlib
import os
import json
import random
import tempfile
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping


class RegressionStatus(StrEnum):
    PASSED = "passed"
    REFERENCE_FAILURE = "reference_failure"
    DUT_FAILURE = "dut_failure"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class ReferenceProvenance:
    """Identity and independence claims for a golden model."""

    model_id: str
    version: str
    source: str
    implementation_language: str = "python"
    generated_by: str = "human"
    independent_of_rtl: bool = True


@dataclass(frozen=True)
class ReferenceModel:
    name: str
    provenance: ReferenceProvenance
    evaluate: Callable[[Mapping[str, Any]], Mapping[str, Any]] = field(
        repr=False, compare=False
    )


@dataclass(frozen=True)
class RegressionVector:
    vector_id: str
    inputs: Mapping[str, Any]
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class RegressionCase:
    case_id: str
    model: str
    model_version: str
    vectors: tuple[RegressionVector, ...]
    source: str = "seeded"
    description: str = ""


@dataclass(frozen=True)
class Mismatch:
    vector_id: str
    inputs: Mapping[str, Any]
    expected: Mapping[str, Any]
    actual: Mapping[str, Any] | None
    kind: str
    error: str | None = None


@dataclass(frozen=True)
class RegressionResult:
    status: RegressionStatus
    model: str
    case_id: str
    vectors_run: int
    mismatches: tuple[Mismatch, ...] = ()
    reference_error: str | None = None
    dut_error: str | None = None


def _u(value: Any, bits: int) -> int:
    return int(value) & ((1 << bits) - 1)


def _alu8(inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    """Combinational 8-bit ALU model used as a trusted seed block."""

    a, b, op = _u(inputs["a"], 8), _u(inputs["b"], 8), str(inputs["op"])
    if op == "add":
        full = a + b
        return {"y": full & 0xFF, "carry": int(full > 0xFF), "zero": int((full & 0xFF) == 0)}
    if op == "sub":
        return {"y": (a - b) & 0xFF, "carry": int(a >= b), "zero": int(((a - b) & 0xFF) == 0)}
    if op == "and":
        y = a & b
    elif op == "or":
        y = a | b
    elif op == "xor":
        y = a ^ b
    elif op == "not":
        y = (~a) & 0xFF
    else:
        raise ValueError(f"unsupported ALU operation: {op}")
    return {"y": y, "carry": 0, "zero": int(y == 0)}


def _counter8(inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    """One-cycle next-state model for an enabled synchronous counter."""

    value = _u(inputs["value"], 8)
    if bool(inputs.get("reset", False)):
        nxt = 0
    elif bool(inputs.get("enable", True)):
        nxt = (value + 1) & 0xFF
    else:
        nxt = value
    return {"value": nxt, "wrap": int(value == 0xFF and bool(inputs.get("enable", True)) and not bool(inputs.get("reset", False)))}


def _arbiter4(inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    """Fixed-priority, one-hot arbiter (request bit 0 has highest priority)."""

    request = _u(inputs["request"], 4)
    grant = 0
    for bit in range(4):
        if request & (1 << bit):
            grant = 1 << bit
            break
    return {"grant": grant, "valid": int(grant != 0)}


_MODELS: dict[str, ReferenceModel] = {
    "alu8": ReferenceModel(
        "alu8",
        ReferenceProvenance("golden.alu8", "1.0.0", "backend/golden_models.py"),
        _alu8,
    ),
    "counter8": ReferenceModel(
        "counter8",
        ReferenceProvenance("golden.counter8", "1.0.0", "backend/golden_models.py"),
        _counter8,
    ),
    "arbiter4": ReferenceModel(
        "arbiter4",
        ReferenceProvenance("golden.arbiter4", "1.0.0", "backend/golden_models.py"),
        _arbiter4,
    ),
}


def get_reference_model(name: str) -> ReferenceModel:
    try:
        return _MODELS[name]
    except KeyError as exc:
        raise KeyError(f"unknown reference model: {name}") from exc


def list_reference_models() -> tuple[ReferenceModel, ...]:
    return tuple(_MODELS.values())


def _vectors_for(name: str, rng: random.Random, count: int) -> list[Mapping[str, Any]]:
    if name == "alu8":
        directed = [
            {"a": 0, "b": 0, "op": "add"},
            {"a": 255, "b": 1, "op": "add"},
            {"a": 0, "b": 1, "op": "sub"},
            {"a": 255, "b": 255, "op": "xor"},
            {"a": 0, "b": 255, "op": "not"},
        ]
        randoms = [{"a": rng.randrange(256), "b": rng.randrange(256), "op": rng.choice(("add", "sub", "and", "or", "xor", "not"))} for _ in range(max(0, count - len(directed)))]
    elif name == "counter8":
        directed = [
            {"value": 0, "enable": False, "reset": False},
            {"value": 0, "enable": True, "reset": False},
            {"value": 255, "enable": True, "reset": False},
            {"value": 255, "enable": True, "reset": True},
        ]
        randoms = [{"value": rng.randrange(256), "enable": bool(rng.randrange(2)), "reset": bool(rng.randrange(2))} for _ in range(max(0, count - len(directed)))]
    elif name == "arbiter4":
        directed = [{"request": r} for r in (0, 1, 2, 4, 8, 3, 12, 15)]
        randoms = [{"request": rng.randrange(16)} for _ in range(max(0, count - len(directed)))]
    else:
        raise KeyError(name)
    return (directed + randoms)[:count]


def seed_case(model_name: str, *, seed: int = 1, count: int = 32, case_id: str | None = None) -> RegressionCase:
    """Create deterministic directed-plus-random vectors for a trusted model."""

    if count <= 0:
        raise ValueError("count must be positive")
    model = get_reference_model(model_name)
    inputs = _vectors_for(model_name, random.Random(seed), count)
    vectors = tuple(
        RegressionVector(f"{model_name}-{index:04d}", item, model.evaluate(item))
        for index, item in enumerate(inputs)
    )
    return RegressionCase(
        case_id=case_id or f"{model_name}-seed-{seed}",
        model=model_name,
        model_version=model.provenance.version,
        vectors=vectors,
        description=f"seed={seed}; directed corners plus deterministic random vectors",
    )


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def run_case(case: RegressionCase, dut: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> RegressionResult:
    """Run a case against a DUT adapter and classify failures explicitly."""

    model = get_reference_model(case.model)
    if not model.provenance.independent_of_rtl:
        return RegressionResult(RegressionStatus.REFERENCE_FAILURE, case.model, case.case_id, 0, reference_error="reference model is not marked independent of RTL")
    if case.model_version != model.provenance.version:
        return RegressionResult(RegressionStatus.REFERENCE_FAILURE, case.model, case.case_id, 0, reference_error=f"case expects reference version {case.model_version}, current is {model.provenance.version}")
    mismatches: list[Mismatch] = []
    for vector in case.vectors:
        try:
            expected = dict(model.evaluate(vector.inputs))
        except Exception as exc:  # reference failure must not be blamed on the DUT
            return RegressionResult(RegressionStatus.REFERENCE_FAILURE, case.model, case.case_id, 0, reference_error=f"{type(exc).__name__}: {exc}")
        if _json_hash(expected) != _json_hash(vector.expected):
            return RegressionResult(RegressionStatus.REFERENCE_FAILURE, case.model, case.case_id, 0, reference_error=f"stored expected output changed for {vector.vector_id}")
        try:
            actual = dict(dut(vector.inputs))
        except Exception as exc:
            return RegressionResult(RegressionStatus.DUT_FAILURE, case.model, case.case_id, len(mismatches) + 1, mismatches=tuple(mismatches + [Mismatch(vector.vector_id, vector.inputs, expected, None, "dut_exception", f"{type(exc).__name__}: {exc}")]), dut_error=f"{type(exc).__name__}: {exc}")
        if actual != expected:
            mismatches.append(Mismatch(vector.vector_id, vector.inputs, expected, actual, "output_mismatch"))
    return RegressionResult(RegressionStatus.DUT_FAILURE if mismatches else RegressionStatus.PASSED, case.model, case.case_id, len(case.vectors), tuple(mismatches))


def run_corpus(
    corpus: "RegressionCorpus",
    dut_by_model: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]],
) -> tuple[RegressionResult, ...]:
    """Run every persisted case, marking missing DUT adapters inconclusive."""

    results: list[RegressionResult] = []
    for case in corpus.cases():
        dut = dut_by_model.get(case.model)
        if dut is None:
            results.append(RegressionResult(RegressionStatus.INCONCLUSIVE, case.model, case.case_id, 0, dut_error=f"no DUT adapter registered for {case.model}"))
        else:
            results.append(run_case(case, dut))
    return tuple(results)


class RegressionCorpus:
    """Persistent JSON corpus for seeded cases and discovered regressions."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def save(self, case: RegressionCase) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{case.case_id}.json"
        payload = {
            "schema_version": 1,
            "case": {
                "case_id": case.case_id,
                "model": case.model,
                "model_version": case.model_version,
                "source": case.source,
                "description": case.description,
                "vectors": [asdict(vector) for vector in case.vectors],
            },
            "reference_provenance": asdict(get_reference_model(case.model).provenance),
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.root, delete=False) as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            tmp = Path(stream.name)
        tmp.replace(target)
        try:
            dir_fd = os.open(self.root, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        return target

    def load(self, path: str | Path) -> RegressionCase:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported regression corpus schema")
        raw = payload["case"]
        return RegressionCase(
            raw["case_id"], raw["model"], raw["model_version"],
            tuple(RegressionVector(v["vector_id"], v["inputs"], v["expected"]) for v in raw["vectors"]),
            raw.get("source", "seeded"), raw.get("description", ""),
        )

    def cases(self) -> tuple[RegressionCase, ...]:
        return tuple(self.load(path) for path in sorted(self.root.glob("*.json"))) if self.root.exists() else ()

    def add_discovered_bug(self, *, model: str, inputs: Mapping[str, Any], case_id: str, description: str) -> Path:
        reference = get_reference_model(model)
        vector = RegressionVector(f"{case_id}-0000", dict(inputs), reference.evaluate(inputs))
        return self.save(RegressionCase(case_id, model, reference.provenance.version, (vector,), source="discovered_bug", description=description))


def seeded_corpus(root: str | Path, *, seed: int = 1, count: int = 32) -> RegressionCorpus:
    corpus = RegressionCorpus(root)
    for name in ("alu8", "counter8", "arbiter4"):
        case_id = f"{name}-seed-{seed}"
        if not (Path(root) / f"{case_id}.json").exists():
            corpus.save(seed_case(name, seed=seed, count=count, case_id=case_id))
    return corpus

