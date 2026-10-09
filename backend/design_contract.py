"""Compatibility/public surface for the versioned DesignSpec-backed contract.

The implementation lives in :mod:`genui` so the state model and restricted UI
protocol cannot diverge.
"""
from genui import (
    COMPONENT_CATALOG, ContractDecision, ContractProtocolError, DesignContract,
    GENUI_SCHEMA_VERSION, SCHEMA_VERSION, apply_action, apply_component_ops,
    components_for, extract_contract, state_from, state_payload, validate_component,
)

# Descriptive aliases used by callers that refer to the contract as a spec.
DesignContractState = DesignContract
extract_design_contract = extract_contract

__all__ = [
    "COMPONENT_CATALOG", "ContractDecision", "ContractProtocolError",
    "DesignContract", "DesignContractState", "GENUI_SCHEMA_VERSION",
    "SCHEMA_VERSION", "apply_action", "apply_component_ops", "components_for",
    "extract_contract", "extract_design_contract", "state_from", "state_payload",
    "validate_component",
]
