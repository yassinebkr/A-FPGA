"""Restricted, typed GenUI protocol for the design contract workbench.

The model/provider is not allowed to send executable UI.  Components are
selected from :data:`COMPONENT_CATALOG`, and all mutations are validated and
hashed by this module before they can become project state.  The protocol is
deliberately independent from the EDA tools: a contract action only changes
the design-contract draft and its review state.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from types import MappingProxyType
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from evidence import DesignSpec, canonical_data, canonical_json


SCHEMA_VERSION = "design-contract/v1"
GENUI_SCHEMA_VERSION = "genui/v1"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ContractProtocolError(ValueError):
    """A client supplied contract/UI message is invalid or stale."""

    def __init__(self, message: str, *, code: str = "invalid_contract"):
        super().__init__(message)
        self.code = code


class ContractDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=1000)
    options: list[str] = Field(default_factory=list, max_length=32)
    required: bool = True

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            raise ValueError("decision id contains unsupported characters")
        return value

    @field_validator("options")
    @classmethod
    def valid_options(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("decision options must be unique")
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError("decision options must be non-empty strings")
        return values


class DesignContract(BaseModel):
    """Versioned project-owned contract state backed by one ``DesignSpec``.

    ``digest`` is a content digest, not a client supplied identifier.  It is
    recomputed on every mutation and is used with ``revision`` for optimistic
    concurrency checks from the browser.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    project_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    # ``locked`` is the only state that authorizes generated RTL/EDA work.
    # Legacy states remain accepted so Phase 2 project files can reload.
    status: Literal["draft", "needs_decision", "ready", "locked",
                    "approved", "revision_requested"] = "draft"
    spec: DesignSpec
    decisions: list[ContractDecision] = Field(default_factory=list)
    answers: dict[str, str] = Field(default_factory=dict)
    paused_reason: str | None = None
    pending_request: bool = False
    digest: str = ""

    @field_validator("digest")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if value and not _DIGEST.fullmatch(value):
            raise ValueError("digest must be a lowercase SHA-256 digest")
        return value

    def payload_for_digest(self) -> dict[str, Any]:
        value = self.model_dump(mode="json", exclude={"digest"})
        return value

    def recompute_digest(self) -> str:
        return hashlib.sha256(canonical_json(self.payload_for_digest()).encode("utf-8")).hexdigest()

    def with_digest(self) -> "DesignContract":
        value = self.model_copy(update={"digest": self.recompute_digest()})
        return value

    def canonical(self) -> dict[str, Any]:
        value = self.with_digest()
        return value.model_dump(mode="json")


# The catalog is intentionally immutable and small.  ``kind`` is the only
# rendering primitive the client receives; arbitrary HTML/CSS/JS is never a
# valid component representation.
COMPONENT_CATALOG = MappingProxyType({
    "contract.summary": MappingProxyType({
        "kind": "summary",
        "props": frozenset({"name", "design_id", "target", "top_module", "status"}),
    }),
    "contract.interfaces": MappingProxyType({
        "kind": "interface_list",
        "props": frozenset({"items"}),
    }),
    "contract.requirements": MappingProxyType({
        "kind": "requirement_list",
        "props": frozenset({"items"}),
    }),
    "contract.decisions": MappingProxyType({
        "kind": "decision_list",
        "props": frozenset({"items", "answers"}),
    }),
    "contract.actions": MappingProxyType({
        "kind": "action_bar",
        "props": frozenset({"actions"}),
    }),
})

_FORBIDDEN_UI_KEYS = frozenset({
    "html", "css", "style", "javascript", "js", "script", "markup",
    "template", "component_definition", "define",
})
_ALLOWED_ACTIONS = frozenset({
    "contract.approve", "contract.approve_and_build", "contract.revise",
    "contract.answer_decision",
})


def _reject_executable_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_UI_KEYS:
                raise ContractProtocolError(
                    f"arbitrary UI field '{key}' is not allowed", code="forbidden_ui")
            _reject_executable_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_executable_keys(child)


def validate_component(component: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a catalog component without executable UI fields."""
    if not isinstance(component, Mapping):
        raise ContractProtocolError("component must be an object", code="invalid_component")
    _reject_executable_keys(component)
    component_id = component.get("id")
    if not isinstance(component_id, str) or component_id not in COMPONENT_CATALOG:
        raise ContractProtocolError("component id is not in the static catalog", code="unknown_component")
    entry = COMPONENT_CATALOG[component_id]
    if component.get("kind") != entry["kind"]:
        raise ContractProtocolError("component kind does not match the catalog", code="invalid_component")
    props = component.get("props", {})
    if not isinstance(props, Mapping):
        raise ContractProtocolError("component props must be an object", code="invalid_component")
    unknown = set(props) - set(entry["props"])
    if unknown:
        raise ContractProtocolError(
            f"unsupported props for {component_id}: {', '.join(sorted(unknown))}",
            code="invalid_component")
    result = {"id": component_id, "kind": entry["kind"], "props": deepcopy(dict(props))}
    # Components can expose only typed, named client actions.  The backend
    # still revalidates the action when it receives it.
    if "actions" in component:
        actions = component["actions"]
        if not isinstance(actions, list) or any(item not in _ALLOWED_ACTIONS for item in actions):
            raise ContractProtocolError("component has an unsupported action", code="invalid_action")
        result["actions"] = list(actions)
    return result


def _spec_from(value: DesignSpec | Mapping[str, Any]) -> DesignSpec:
    if isinstance(value, DesignSpec):
        return value
    try:
        parse = getattr(DesignSpec, "model_validate", None)
        return parse(value) if parse else DesignSpec.parse_obj(value)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ContractProtocolError(f"invalid DesignSpec: {exc}", code="invalid_spec") from exc


def _decisions_from_spec(spec: DesignSpec) -> list[ContractDecision]:
    raw = spec.metadata.get("decisions", []) if isinstance(spec.metadata, Mapping) else []
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ContractProtocolError("DesignSpec metadata.decisions must be a list", code="invalid_spec")
    decisions: list[ContractDecision] = []
    for item in raw:
        if isinstance(item, str):
            item = {"id": item, "question": item}
        try:
            decisions.append(ContractDecision.model_validate(item))
        except (ValidationError, TypeError, ValueError) as exc:
            raise ContractProtocolError(f"invalid contract decision: {exc}", code="invalid_spec") from exc
    return decisions


def components_for(contract: DesignContract) -> list[dict[str, Any]]:
    """Derive the complete authoritative component set from contract state."""
    spec = contract.spec
    components = [
        {"id": "contract.summary", "kind": "summary", "props": {
            "name": spec.name, "design_id": spec.design_id, "target": spec.target,
            "top_module": spec.top_module, "status": contract.status,
        }, "actions": ["contract.approve", "contract.revise"]},
        {"id": "contract.interfaces", "kind": "interface_list", "props": {
            "items": [item.model_dump(mode="json") for item in spec.interfaces],
        }},
        {"id": "contract.requirements", "kind": "requirement_list", "props": {
            "items": [item.model_dump(mode="json") for item in spec.requirements],
        }},
        {"id": "contract.decisions", "kind": "decision_list", "props": {
            "items": [item.model_dump(mode="json") for item in contract.decisions],
            "answers": dict(contract.answers),
        }, "actions": ["contract.answer_decision"]},
        {"id": "contract.actions", "kind": "action_bar", "props": {
            "actions": sorted(_ALLOWED_ACTIONS),
        }},
    ]
    return [validate_component(item) for item in components]


def extract_contract(spec: DesignSpec | Mapping[str, Any], *, project_id: str,
                     session_id: str | None = None) -> dict[str, Any]:
    """Create a new draft contract state from a validated ``DesignSpec``."""
    if not isinstance(project_id, str) or not project_id:
        raise ContractProtocolError("project_id is required", code="invalid_project")
    session_id = session_id or project_id
    parsed = _spec_from(spec)
    decisions = _decisions_from_spec(parsed)
    unresolved = any(item.required for item in decisions)
    contract = DesignContract(
        project_id=project_id, session_id=session_id, spec=parsed,
        decisions=decisions,
        status="needs_decision" if unresolved else "ready",
    ).with_digest()
    return state_payload(contract)


def state_from(value: DesignContract | Mapping[str, Any]) -> DesignContract:
    try:
        contract = value if isinstance(value, DesignContract) else DesignContract.model_validate(value)
        expected = contract.recompute_digest()
        if contract.digest != expected:
            raise ContractProtocolError("contract digest does not match its contents", code="invalid_digest")
        return contract
    except ContractProtocolError:
        raise
    except (ValidationError, TypeError, ValueError) as exc:
        raise ContractProtocolError(f"invalid contract state: {exc}", code="invalid_contract") from exc


def state_payload(contract: DesignContract | Mapping[str, Any]) -> dict[str, Any]:
    value = state_from(contract) if isinstance(contract, Mapping) else contract.with_digest()
    return {
        "schema_version": GENUI_SCHEMA_VERSION,
        "contract_schema_version": SCHEMA_VERSION,
        "project": value.project_id,
        "session": value.session_id,
        "revision": value.revision,
        "digest": value.digest,
        "status": value.status,
        "contract": value.canonical(),
        "components": components_for(value),
        "authoritative": True,
    }


def _check_expected(current: DesignContract, *, project_id: str, session_id: str,
                    expected_revision: Any, expected_digest: Any) -> None:
    if current.project_id != project_id or current.session_id != session_id:
        raise ContractProtocolError("contract belongs to a different project/session", code="project_mismatch")
    if expected_revision is None or expected_digest is None:
        raise ContractProtocolError("expected_revision and expected_digest are required", code="precondition_required")
    try:
        revision = int(expected_revision)
    except (TypeError, ValueError) as exc:
        raise ContractProtocolError("expected_revision must be an integer", code="invalid_revision") from exc
    if revision != current.revision:
        raise ContractProtocolError("contract revision is stale", code="stale_revision")
    if expected_digest != current.digest:
        raise ContractProtocolError("contract digest is stale", code="stale_digest")


def apply_action(current: DesignContract | Mapping[str, Any], *, action: str,
                 project_id: str, session_id: str, expected_revision: Any,
                 expected_digest: Any, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Apply one typed contract action and return authoritative state.

    This function has no imports from ``eda_tools`` and performs no process or
    job submission.  A caller may broadcast the returned state over WebSocket.
    """
    if action not in _ALLOWED_ACTIONS:
        raise ContractProtocolError("unsupported contract action", code="invalid_action")
    current = state_from(current)
    _check_expected(current, project_id=project_id, session_id=session_id,
                    expected_revision=expected_revision, expected_digest=expected_digest)
    payload = dict(payload or {})
    _reject_executable_keys(payload)
    updated = current.model_copy(deep=True)
    if action in {"contract.approve", "contract.approve_and_build"}:
        if current.status in {"locked", "approved"}:
            # Exact retries are idempotent. _check_expected above still rejects
            # stale revision/digest actions, so this cannot approve a different
            # contract or start a second continuation.
            return state_payload(current)
        unresolved = [item.id for item in current.decisions
                      if item.required and not str(current.answers.get(item.id, "")).strip()]
        if unresolved:
            raise ContractProtocolError(
                "required contract decisions are unanswered: " + ", ".join(unresolved),
                code="decision_required")
        updated.status = "locked"
        updated.paused_reason = None
    elif action == "contract.revise":
        changes = payload.get("spec", payload.get("changes", payload.get("patch")))
        if changes is None:
            raise ContractProtocolError("contract.revise requires spec, changes, or patch", code="invalid_spec")
        if not isinstance(changes, Mapping):
            raise ContractProtocolError("contract.revise changes must be an object", code="invalid_spec")
        merged = updated.spec.model_dump(mode="json")
        changes = dict(changes)
        # The compact card edits architecture fields. Preserve DesignSpec as
        # the canonical record while merging those fields into its typed
        # target/timing/metadata sections.
        if "top_module" in changes:
            merged["top_module"] = changes.pop("top_module")
        if "technology" in changes:
            merged["target"] = changes["technology"]
        elif "target" in changes:
            merged["target"] = changes["target"]
        timing_keys = {
            "clock_target_mhz", "clock_period_ns", "latency_cycles",
            "throughput_per_cycle", "pipeline_stages",
        }
        timing = dict(merged.get("timing_goals") or {})
        for key in timing_keys:
            if key in changes:
                timing[key] = changes[key]
        merged["timing_goals"] = timing
        metadata = dict(merged.get("metadata") or {})
        architecture = dict(metadata.get("architecture_contract") or {})
        architecture.update({key: value for key, value in changes.items()
                             if key in architecture})
        if architecture:
            metadata["architecture_contract"] = architecture
        merged["metadata"] = metadata
        # Full DesignSpec callers may still revise top-level typed fields.
        for key, value in changes.items():
            if key in merged and key not in {"metadata", "timing_goals"}:
                merged[key] = value
        updated.spec = _spec_from(merged)
        updated.decisions = _decisions_from_spec(updated.spec)
        updated.answers = {}
        unresolved = any(item.required for item in updated.decisions)
        updated.status = "needs_decision" if unresolved else "ready"
        updated.paused_reason = "awaiting_contract_approval"
    else:  # contract.answer_decision
        decision_id = payload.get("decision_id", payload.get("id"))
        answer = payload.get("answer")
        if not isinstance(decision_id, str) or not isinstance(answer, str) or not answer.strip():
            raise ContractProtocolError("answer_decision requires decision_id and answer", code="invalid_decision")
        decision = next((item for item in updated.decisions if item.id == decision_id), None)
        if decision is None:
            raise ContractProtocolError("unknown contract decision", code="unknown_decision")
        if decision.options and answer not in decision.options:
            raise ContractProtocolError("answer is not one of the decision options", code="invalid_decision")
        updated.answers[decision_id] = answer
        if updated.status in {"locked", "approved"}:
            raise ContractProtocolError(
                "locked contract must be revised before answering decisions",
                code="contract_locked")
        unresolved = any(item.required and not str(updated.answers.get(item.id, "")).strip()
                         for item in updated.decisions)
        updated.status = "needs_decision" if unresolved else "ready"
    updated.revision = current.revision + 1
    updated.digest = updated.recompute_digest()
    return state_payload(updated)


def apply_component_ops(components: list[Mapping[str, Any]], ops: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Apply catalog-only ``upsert``, ``patch`` or ``remove`` operations.

    This is useful for server-side rendering adapters.  It is intentionally
    not an EDA or HTML definition API; every resulting component still comes
    from the static catalog.
    """
    if not isinstance(ops, list):
        raise ContractProtocolError("ops must be a list", code="invalid_operation")
    current = {item["id"]: validate_component(item) for item in components}
    for operation in ops:
        if not isinstance(operation, Mapping):
            raise ContractProtocolError("component operation must be an object", code="invalid_operation")
        _reject_executable_keys(operation)
        op = operation.get("op")
        if op == "upsert":
            component = validate_component(operation.get("component", {}))
            current[component["id"]] = component
        elif op == "patch":
            component_id = operation.get("id")
            if component_id not in current:
                raise ContractProtocolError("cannot patch an unknown component", code="unknown_component")
            patch = operation.get("props")
            if not isinstance(patch, Mapping):
                raise ContractProtocolError("patch props must be an object", code="invalid_operation")
            merged = deepcopy(current[component_id])
            merged["props"].update(dict(patch))
            current[component_id] = validate_component(merged)
        elif op == "remove":
            component_id = operation.get("id")
            if component_id not in current:
                raise ContractProtocolError("cannot remove an unknown component", code="unknown_component")
            del current[component_id]
        else:
            raise ContractProtocolError("only upsert, patch, and remove are supported", code="invalid_operation")
    return list(current.values())


__all__ = [
    "SCHEMA_VERSION", "GENUI_SCHEMA_VERSION", "COMPONENT_CATALOG",
    "ContractProtocolError", "ContractDecision", "DesignContract",
    "extract_contract", "state_from", "state_payload", "apply_action",
    "validate_component", "components_for", "apply_component_ops",
]
