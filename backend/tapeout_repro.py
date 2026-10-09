"""Reproducibility and release evidence for Tiny Tapeout hardening runs.

This module deliberately models *evidence* rather than pretending that a local
dry-run is foundry sign-off.  A release candidate is valid only when the exact
inputs, constraints, configuration, tool/container identity and generated
artifacts are recorded, and the required hardening outcomes pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SHA256 = r"^[0-9a-f]{64}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        try:
            value = value.model_dump(mode="json")
        except AttributeError:  # pragma: no cover - pydantic v1 fallback
            value = json.loads(value.json())
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def sha256_record(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def hash_file(path: str | os.PathLike[str]) -> str:
    """Hash one input/artifact without following a directory by accident."""
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"file does not exist: {target}")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ToolIdentity(_Model):
    """Pinned identity of one executable, image, or source checkout."""

    name: str
    version: str
    executable: str | None = None
    source: str | None = None
    digest: str | None = None
    invocation: str | None = None

    @field_validator("name", "version")
    @classmethod
    def required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("tool identity text must not be blank")
        return value

    @field_validator("digest")
    @classmethod
    def valid_digest(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)):
            raise ValueError("tool digest must be a lowercase SHA-256 digest")
        return value


class ContainerIdentity(_Model):
    image: str
    digest: str
    runtime: str | None = None
    args: list[str] = Field(default_factory=list)

    @field_validator("image")
    @classmethod
    def valid_image(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("container image must not be blank")
        return value

    @field_validator("digest")
    @classmethod
    def valid_container_digest(cls, value: str) -> str:
        if not value.startswith("sha256:") or len(value) != 71:
            raise ValueError("container digest must be a pinned sha256: digest")
        return value


class InputHash(_Model):
    path: str
    sha256: str
    role: Literal["rtl", "testbench", "constraint", "config", "pdk", "script", "other"] = "other"
    size_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def portable_path(cls, value: str) -> str:
        value = value.replace("\\", "/").strip()
        if not value or value.startswith("/") or ".." in value.split("/"):
            raise ValueError("input path must be a non-empty relative path")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_input_hash(cls, value: str) -> str:
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("input sha256 must be a lowercase SHA-256 digest")
        return value


class ArtifactInventoryItem(_Model):
    path: str
    sha256: str
    size_bytes: int = Field(ge=0)
    kind: Literal["gds", "lef", "def", "netlist", "report", "log", "manifest", "archive", "other"] = "other"
    required: bool = True

    @field_validator("path")
    @classmethod
    def artifact_path(cls, value: str) -> str:
        value = value.replace("\\", "/").strip()
        if not value or value.startswith("/") or ".." in value.split("/"):
            raise ValueError("artifact path must be a non-empty relative path")
        return value

    @field_validator("sha256")
    @classmethod
    def artifact_hash(cls, value: str) -> str:
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("artifact sha256 must be a lowercase SHA-256 digest")
        return value


StageStatus = Literal["passed", "failed", "inconclusive", "not_run", "not_applicable"]


class HardeningStage(_Model):
    name: Literal["drc", "lvs", "timing", "antenna", "erc", "power", "wrapper", "test"]
    status: StageStatus
    tool: str | None = None
    tool_version: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    artifact_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_false_pass(self):
        # A stage cannot be marked passed while explicitly carrying an error.
        if self.status == "passed" and self.details.get("error"):
            raise ValueError(f"{self.name} cannot be passed with an error")
        return self


class TinyTapeoutRun(_Model):
    """Complete, immutable-ready evidence record for one hardening attempt."""

    schema_version: str = "tt-repro/v1"
    run_id: str
    design_id: str
    template: str = "ttsky-verilog-template"
    process: str = "sky130"
    top_module: str
    toolchain: list[ToolIdentity] = Field(default_factory=list)
    container: ContainerIdentity | None = None
    inputs: list[InputHash] = Field(default_factory=list)
    config_sha256: str | None = None
    constraints_sha256: str | None = None
    stages: list[HardeningStage] = Field(default_factory=list)
    artifacts: list[ArtifactInventoryItem] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    source_revision: str | None = None
    rerun_of: str | None = None
    # Producer-declared assurance is explicit metadata, not something inferred
    # from the presence of a container. The default keeps older serialized
    # run records conservative when they are loaded after this field is added.
    assurance: Literal["local_dry_run", "provider_hardening", "foundry_signoff"] = "local_dry_run"
    status: Literal["planned", "completed", "failed", "inconclusive"] = "planned"

    @field_validator("run_id", "design_id", "top_module")
    @classmethod
    def nonempty_identity(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("run identity must not be blank")
        return value

    @field_validator("config_sha256", "constraints_sha256")
    @classmethod
    def optional_hash(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)):
            raise ValueError("declared hash must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_evidence_shape(self):
        if self.status == "completed" and not self.toolchain:
            raise ValueError("completed run requires pinned tool identities")
        if self.status == "completed" and not self.inputs:
            raise ValueError("completed run requires input hashes")
        return self

    def digest(self) -> str:
        return sha256_record(self)


class TapeoutEvidenceRepository:
    """Small immutable store for the TT-specific evidence record.

    The generic EvidenceRepository stores RunManifest. A TT hardening run
    has a different, narrower contract, so it is kept in a sibling canonical
    JSON store rather than coerced into a generic manifest.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None):
        configured = root or os.environ.get("TAPEOUT_EVIDENCE_ROOT")
        if configured is None:
            configured = Path(__file__).resolve().parent / "data" / "evidence" / "tapeout-runs"
        self.root = Path(configured).expanduser().absolute()
        self.runs = self.root / "runs"

    @staticmethod
    def _safe_run_id(run_id: str) -> str:
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
            raise ValueError("invalid Tiny Tapeout evidence run id")
        return run_id

    def save(self, run: TinyTapeoutRun, gate: "ReleaseGate") -> tuple[str, Path]:
        run_id = self._safe_run_id(run.run_id)
        content = canonical_json({"run": run.model_dump(mode="json"), "release_gate": gate.model_dump(mode="json")}).encode("utf-8")
        digest = sha256_bytes(content)
        path = self.runs / f"{run_id}.json"
        self.runs.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != content:
            raise ValueError(f"Tiny Tapeout evidence is immutable: {run_id}")
        if not path.exists():
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.runs, prefix=f".{run_id}.",
                                                 suffix=".tmp", delete=False) as handle:
                    temporary = handle.name
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                temporary = None
                try:
                    directory_fd = os.open(self.runs, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            finally:
                if temporary:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
        return digest, path

    def load(self, run_id: str) -> dict[str, Any] | None:
        path = self.runs / f"{self._safe_run_id(run_id)}.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None


class RerunDifference(_Model):
    field: str
    expected: Any = None
    actual: Any = None
    severity: Literal["error", "warning"] = "error"


class RerunComparison(_Model):
    equivalent: bool
    differences: list[RerunDifference] = Field(default_factory=list)
    compared_fields: list[str] = Field(default_factory=list)


class ReleaseGate(_Model):
    eligible: bool
    reasons: list[str] = Field(default_factory=list)
    required_stages: list[str] = Field(default_factory=lambda: ["drc", "lvs", "timing"])
    evidence_digest: str | None = None
    rerun_equivalent: bool | None = None
    assurance: Literal["local_dry_run", "provider_hardening", "foundry_signoff"] = "local_dry_run"




def inventory_directory(root: str | os.PathLike[str], *, required_suffixes: Mapping[str, str] | None = None) -> list[ArtifactInventoryItem]:
    """Inventory regular files below ``root`` with deterministic relative paths."""
    base = Path(root).resolve()
    if not base.is_dir():
        raise ValueError(f"artifact root does not exist: {base}")
    items: list[ArtifactInventoryItem] = []
    kind_by_suffix = {
        ".gds": "gds", ".lef": "lef", ".def": "def", ".v": "netlist",
        ".sv": "netlist", ".rpt": "report", ".log": "log", ".json": "manifest",
        ".tar": "archive", ".gz": "archive",
    }
    required_suffixes = required_suffixes or {}
    for path in sorted((item for item in base.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        relative = path.relative_to(base).as_posix()
        suffix = path.suffix.lower()
        kind = required_suffixes.get(suffix, kind_by_suffix.get(suffix, "other"))
        items.append(ArtifactInventoryItem(path=relative, sha256=hash_file(path),
                                           size_bytes=path.stat().st_size, kind=kind))
    return items


def compare_reruns(first: TinyTapeoutRun, second: TinyTapeoutRun) -> RerunComparison:
    """Compare reproducibility-critical fields, not timestamps or run ids."""
    fields = ("design_id", "template", "process", "top_module", "toolchain",
              "container", "inputs", "config_sha256", "constraints_sha256",
              "artifacts", "stages", "assurance")
    differences: list[RerunDifference] = []
    for field in fields:
        left = getattr(first, field)
        right = getattr(second, field)
        left_data = left.model_dump(mode="json") if isinstance(left, BaseModel) else [x.model_dump(mode="json") for x in left] if isinstance(left, list) else left
        right_data = right.model_dump(mode="json") if isinstance(right, BaseModel) else [x.model_dump(mode="json") for x in right] if isinstance(right, list) else right
        if left_data != right_data:
            differences.append(RerunDifference(field=field, expected=left_data, actual=right_data))
    return RerunComparison(equivalent=not differences, differences=differences,
                           compared_fields=list(fields))


def evaluate_release_gate(run: TinyTapeoutRun, *, rerun: TinyTapeoutRun | None = None,
                          require_artifacts: bool = True,
                          require_container: bool = False) -> ReleaseGate:
    """Fail closed unless reproducibility and mandatory hardening evidence pass."""
    reasons: list[str] = []
    required = ["drc", "lvs", "timing"]
    if run.status != "completed":
        reasons.append(f"run status is {run.status}, not completed")
    if not run.toolchain and run.container is None:
        reasons.append("no pinned toolchain or container identity")
    if require_container and run.container is None:
        reasons.append("container image digest is missing")
    if not run.inputs:
        reasons.append("input hash inventory is missing")
    if not run.config_sha256:
        reasons.append("configuration hash is missing")
    if not run.constraints_sha256:
        reasons.append("constraints hash is missing")
    if require_artifacts and not run.artifacts:
        reasons.append("generated artifact inventory is missing")
    stages = {stage.name: stage for stage in run.stages}
    for name in required:
        stage = stages.get(name)
        if stage is None:
            reasons.append(f"required stage {name} is missing")
        elif stage.status != "passed":
            reasons.append(f"required stage {name} is {stage.status}")
    rerun_equivalent: bool | None = None
    if rerun is not None:
        comparison = compare_reruns(run, rerun)
        rerun_equivalent = comparison.equivalent
        if not comparison.equivalent:
            reasons.extend(f"rerun mismatch: {item.field}" for item in comparison.differences)
    return ReleaseGate(eligible=not reasons, reasons=reasons,
                       required_stages=required, evidence_digest=run.digest(),
                       rerun_equivalent=rerun_equivalent,
                       assurance=run.assurance)


def _pdk_manifest_identity(root: Path, pdk_name: str | None) -> tuple[str, int, str] | None:
    """Return an exact hash for a small, pinned PDK identity manifest.

    A full PDK tree is gigabytes and hashing it on every run is both wasteful
    and a source of surprising long-running jobs. The hardening tool itself
    must expose a pinned manifest/version file; absence is recorded as
    inconclusive instead of being silently replaced by a directory hash.
    """
    names = ["pdk.json", "manifest.json", "version.txt", "VERSION"]
    candidates = [root / name for name in names]
    if pdk_name:
        candidates.extend(root.glob(f"{pdk_name}/pdk.json"))
        candidates.extend(root.glob(f"{pdk_name}/manifest.json"))
        candidates.extend(root.glob(f"{pdk_name}/version.txt"))
    manifests = [path for path in candidates if path.is_file()]
    if manifests:
        records = [{"path": path.relative_to(root).as_posix(),
                    "sha256": hash_file(path), "size_bytes": path.stat().st_size}
                   for path in sorted(set(manifests))]
        return sha256_record(records), sum(item["size_bytes"] for item in records), f"pdk/{pdk_name or root.name}"

    # Ciel stores PDKs below a uniquely pinned 40-hex revision. A selected
    # PDK is exact only when that layout contains one unambiguous revision.
    revisions = sorted({
        candidate for candidate in root.glob("**/versions/*")
        if candidate.is_dir()
        and re.fullmatch(r"[0-9a-f]{40}", candidate.name)
        and (candidate / (pdk_name or "")).is_dir()
    })
    if len(revisions) != 1:
        return None
    revision = revisions[0].name
    identity = {"family": revisions[0].parent.parent.name,
                "revision": revision, "pdk": pdk_name}
    return sha256_record(identity), 0, f"pdk/{pdk_name or 'unknown'}@{revision}"


def _input_from_file(path: Path, workspace: Path, role: str) -> InputHash:
    return InputHash(path=path.relative_to(workspace).as_posix(),
                     sha256=hash_file(path), role=role,
                     size_bytes=path.stat().st_size)



def _workspace_stage_outcomes(root: Path) -> dict[str, dict[str, Any]]:
    """Extract only terminal manufacturability metrics.

    Intermediate routing iterations are deliberately ignored. If the terminal
    report or one of its required metrics is absent, the stage is inconclusive.
    """
    candidates = sorted(root.glob("runs/**/74-misc-reportmanufacturability/state_out.json"))
    if not candidates:
        return {}
    state_file = candidates[-1]
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    metrics = state.get("metrics") if isinstance(state, Mapping) else None
    if not isinstance(metrics, Mapping):
        return {}

    def numeric(key: str) -> float | None:
        try:
            return float(metrics[key])
        except (KeyError, TypeError, ValueError):
            return None

    def exact(keys: Sequence[str]) -> tuple[bool, list[dict[str, Any]]]:
        values: list[dict[str, Any]] = []
        for key in keys:
            value = numeric(key)
            if value is None:
                return False, []
            values.append({"metric": key, "value": metrics[key]})
        return True, values

    def prefixed(prefixes: Sequence[str]) -> tuple[bool, list[dict[str, Any]]]:
        keys = sorted(str(key) for key in metrics
                      if any(str(key).startswith(prefix) for prefix in prefixes)
                      and "__iter:" not in str(key))
        if not keys:
            return False, []
        values: list[dict[str, Any]] = []
        for key in keys:
            value = numeric(key)
            if value is None:
                return False, []
            values.append({"metric": key, "value": metrics[key]})
        return True, values

    required: dict[str, tuple[bool, list[dict[str, Any]]]] = {
        "drc": exact(("magic__drc_error__count",
                      "klayout__drc_error__count",
                      "route__drc_errors")),
        "lvs": exact((
            "design__lvs_error__count",
            "design__lvs_device_difference__count",
            "design__lvs_net_difference__count",
            "design__lvs_property_fail__count",
            "design__lvs_unmatched_device__count",
            "design__lvs_unmatched_net__count",
            "design__lvs_unmatched_pin__count",
        )),
        "antenna": exact(("route__antenna_violation__count",
                          "antenna__violating__nets",
                          "antenna__violating__pins")),
    }
    setup_ok, setup_values = prefixed(("timing__setup_vio__count",))
    hold_ok, hold_values = prefixed(("timing__hold_vio__count",))
    required["timing"] = (setup_ok and hold_ok, setup_values + hold_values)

    outcomes: dict[str, dict[str, Any]] = {}
    for stage, (complete, values) in required.items():
        if not complete:
            outcomes[stage] = {
                "status": "inconclusive",
                "source": str(state_file.relative_to(root)),
                "reason": "terminal stage metric(s) missing",
            }
            continue
        status = "failed" if any(float(item["value"]) > 0 for item in values) else "passed"
        outcomes[stage] = {
            "status": status,
            "source": str(state_file.relative_to(root)),
            "metrics": values,
        }
    return outcomes


def _stage_from_text(name: str, output: str, *, check: Mapping[str, Any] | None = None, observed: Mapping[str, Any] | None = None) -> HardeningStage:
    """Classify only explicit tool outcomes; silence is inconclusive."""
    text = output.lower()
    if observed is not None:
        return HardeningStage(name=name, status=str(observed.get("status", "inconclusive")),
                              details=dict(observed), tool="librelane")
    check_status = str((check or {}).get("status", "")).lower()
    if name == "drc" and check_status in {"passed", "failed", "inconclusive", "not_run"}:
        if check_status != "passed":
            return HardeningStage(name=name, status="inconclusive" if check_status in {"inconclusive", "not_run"} else "failed",
                                  tool="klayout", details=dict(check or {}))
        negative = re.search(r"(?:drc|design rule).{0,80}(?:failed|error|violation)[^0-9]{0,20}(?:[1-9][0-9]*)", text)
        return HardeningStage(name=name, status="failed" if negative else "passed",
                              tool="klayout", details=dict(check or {}))
    patterns = {
        "lvs": (r"\blvs\b.{0,100}(?:fail|error|mismatch|not run|skipped)", r"\blvs\b.{0,100}(?:pass|clean|match|0\s+(?:error|mismatch))"),
        "timing": (r"(?:timing|setup|hold).{0,100}(?:fail|violation|error|negative slack)", r"(?:timing|setup|hold).{0,100}(?:pass|clean|met|0\s+(?:violation|error))"),
        "antenna": (r"antenna.{0,100}(?:fail|violation|error)", r"antenna.{0,100}(?:pass|clean|0\s+(?:violation|error))"),
        "erc": (r"\berc\b.{0,100}(?:fail|violation|error)", r"\berc\b.{0,100}(?:pass|clean|0\s+(?:violation|error))"),
    }
    negative, positive = patterns[name]
    if re.search(negative, text, flags=re.IGNORECASE | re.DOTALL):
        status = "failed"
    elif re.search(positive, text, flags=re.IGNORECASE | re.DOTALL):
        status = "passed"
    else:
        status = "inconclusive"
    return HardeningStage(name=name, status=status, details={"parser": "explicit-log-pattern"})


def _tool_identities(result: Mapping[str, Any], workspace: Path | None,
                     config: Any | None) -> list[ToolIdentity]:
    inputs = result.get("inputs") if isinstance(result.get("inputs"), Mapping) else {}
    identities: list[ToolIdentity] = []
    support_revision = inputs.get("support_tools_revision")
    if support_revision:
        identities.append(ToolIdentity(name="tt-support-tools", version=str(support_revision),
                                       source="tt-support-tools", digest=None))
    template_revision = inputs.get("template_revision")
    if template_revision:
        identities.append(ToolIdentity(name="ttsky-verilog-template", version=str(template_revision),
                                       source="ttsky-verilog-template", digest=None))
    try:
        from importlib.metadata import version as package_version
        librelane_version = package_version("librelane")
    except Exception:
        librelane_version = None
    if librelane_version:
        identities.append(ToolIdentity(name="librelane", version=librelane_version,
                                       source="python-package"))
    if workspace is not None:
        tool = workspace / "tt" / "tt_tool.py"
        if tool.is_file():
            identities.append(ToolIdentity(name="tt_tool.py", version=str(support_revision or "unknown"),
                                           executable=str(tool), digest=hash_file(tool)))
    return identities


def _container_identity(result: Mapping[str, Any], output: str) -> ContainerIdentity | None:
    declared = result.get("container")
    if isinstance(declared, Mapping):
        image = declared.get("image")
        digest = declared.get("digest")
        if isinstance(image, str) and isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            return ContainerIdentity(image=image, digest=digest,
                                     runtime=declared.get("runtime"))
    match = re.search(r"(ghcr\.io/[A-Za-z0-9_.@:/-]+).*?(sha256:[0-9a-f]{64})", output,
                      flags=re.IGNORECASE | re.DOTALL)
    if match:
        return ContainerIdentity(image=match.group(1), digest=match.group(2))
    return None


def build_tapeout_evidence(result: Mapping[str, Any], *, wrapper_verilog: str,
                           top_module: str, workspace: str | os.PathLike[str] | None = None,
                           config: Any | None = None, run_id: str | None = None) -> tuple[TinyTapeoutRun, ReleaseGate]:
    """Construct typed evidence from one hardening result, fail-closed."""
    root = Path(workspace).expanduser().resolve() if workspace else None
    output = str(result.get("tool_output") or "")
    checks = result.get("checks") if isinstance(result.get("checks"), Mapping) else {}
    inputs: list[InputHash] = []
    if root and root.is_dir():
        source = root / "src" / f"tt_um_{top_module}.v"
        if source.is_file():
            inputs.append(_input_from_file(source, root, "rtl"))
        for relative, role in (("test/tb.v", "testbench"), ("src/config.json", "config"),
                               ("src/user_config.json", "config"), ("src/pnr.sdc", "constraint"),
                               ("src/signoff.sdc", "constraint")):
            path = root / relative
            if path.is_file():
                inputs.append(_input_from_file(path, root, role))
    if not inputs:
        encoded = wrapper_verilog.encode("utf-8")
        inputs.append(InputHash(path=f"src/tt_um_{top_module}.v",
                                sha256=sha256_bytes(encoded), role="rtl",
                                size_bytes=len(encoded)))
    config_hashes = {item.path: item.sha256 for item in inputs if item.role == "config"}
    constraint_hashes = {item.path: item.sha256 for item in inputs if item.role == "constraint"}
    config_sha256 = sha256_record(config_hashes) if config_hashes else None
    constraints_sha256 = sha256_record(constraint_hashes) if constraint_hashes else None
    pdk_root = None
    pdk_name = None
    if config is not None:
        pdk_name = getattr(config, "pdk", None) or os.environ.get("PDK")
        try:
            pdk_root = getattr(config, "resolved_pdk_root")()
        except (AttributeError, OSError, TypeError):
            raw = os.environ.get("PDK_ROOT")
            pdk_root = Path(raw).expanduser().resolve() if raw else None
    if pdk_root and pdk_root.is_dir():
        pdk_identity = _pdk_manifest_identity(pdk_root, pdk_name)
        if pdk_identity is not None:
            pdk_digest, pdk_size, pdk_label = pdk_identity
            inputs.append(InputHash(path=pdk_label, sha256=pdk_digest,
                                    role="pdk", size_bytes=pdk_size))
    artifacts: list[ArtifactInventoryItem] = []
    raw_artifacts = result.get("artifacts")
    if isinstance(raw_artifacts, list):
        for item in raw_artifacts:
            if not isinstance(item, Mapping):
                continue
            try:
                artifacts.append(ArtifactInventoryItem(path=str(item["path"]),
                    sha256=str(item["sha256"]), size_bytes=int(item["size_bytes"]),
                    kind=str(item.get("kind", "other"))))
            except (KeyError, TypeError, ValueError):
                continue
    stage_names = ("drc", "lvs", "timing", "antenna")
    observed = _workspace_stage_outcomes(root) if root and root.is_dir() else {}
    stages = [_stage_from_text(
        name, output, check=checks.get("klayout_drc") if name == "drc" else None,
        observed=observed.get(name) if root and root.is_dir() else None)
              for name in stage_names]
    status = str(result.get("status", "inconclusive"))
    run_status = "completed" if status == "passed" else ("failed" if status == "failed" else "inconclusive")
    run = TinyTapeoutRun(
        run_id=run_id or f"tt-{uuid.uuid4().hex}",
        design_id=top_module.removeprefix("tt_um_"),
        top_module=top_module if top_module.startswith("tt_um_") else f"tt_um_{top_module}",
        status=run_status, toolchain=_tool_identities(result, root, config),
        container=_container_identity(result, output), inputs=inputs,
        config_sha256=config_sha256, constraints_sha256=constraints_sha256,
        stages=stages, artifacts=artifacts,
        environment={key: str(value) for key, value in (("PDK", pdk_name),)
                     if value},
        source_revision=(result.get("inputs") or {}).get("template_revision")
        if isinstance(result.get("inputs"), Mapping) else None,
        assurance="local_dry_run",
    )
    return run, evaluate_release_gate(run, require_container=True)


def persist_tapeout_evidence(result: Mapping[str, Any], *, wrapper_verilog: str,
                             top_module: str, workspace: str | os.PathLike[str] | None = None,
                             config: Any | None = None, run_id: str | None = None,
                             repository: TapeoutEvidenceRepository | None = None) -> dict[str, Any]:
    run, gate = build_tapeout_evidence(result, wrapper_verilog=wrapper_verilog,
                                        top_module=top_module, workspace=workspace,
                                        config=config, run_id=run_id)
    evidence_repository = repository or TapeoutEvidenceRepository()
    digest, path = evidence_repository.save(run, gate)
    return {"id": run.run_id, "digest": digest, "path": str(path),
            "run": run.model_dump(mode="json"), "release_gate": gate.model_dump(mode="json")}


__all__ = [
    "ArtifactInventoryItem", "ContainerIdentity", "HardeningStage", "InputHash",
    "TapeoutEvidenceRepository", "build_tapeout_evidence", "persist_tapeout_evidence",
    "ReleaseGate", "RerunComparison", "RerunDifference", "TinyTapeoutRun",
    "ToolIdentity", "compare_reruns", "evaluate_release_gate", "hash_file",
    "inventory_directory", "sha256_bytes", "sha256_record",
]
