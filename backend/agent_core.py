"""Agent core: talks to the Kimi API directly via the OpenAI-compatible
chat-completions endpoint and runs a tool-calling loop.

Requires KIMI_API_KEY in the environment or backend/.env. Defaults to the
Kimi Code endpoint (https://api.kimi.com/coding/v1); override with
KIMI_BASE_URL for the platform API (https://api.moonshot.ai/v1).
Model override via KIMI_MODEL (default: k3; "kimi-for-coding" is K2.8).
"""
import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import time
import uuid
from types import SimpleNamespace
from typing import AsyncGenerator

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI

import model_registry
from eda_tools import current_session as _tool_session
from eda_tools import (find_fmax, run_silicon_compiler, run_simulation,
                        run_tiny_tapeout_hardening, run_verification,
                        write_and_check_syntax)
from architecture_contract import (
    ArchitectureContract,
    contract_hash as _architecture_contract_hash,
    define_architecture,
    validate_rtl_against_contract,
)
from project_repository import CorruptProjectError, ProjectRepository
from evidence import DesignSpec, EvidenceError, repository as _evidence_repository
from hardware_project import (
    HardwareProjectError, HardwareProjectUpdate, build_manifest,
    hydrate_manifest, validate_manifest,
)
from module_index import build_module_index
from genui import (
    ContractProtocolError,
    apply_action as _apply_contract_action,
    extract_contract as _extract_design_contract,
    state_from as _contract_state_from,
)
from timing_intent import validate_timing_retry_intent
from timing_optimizer import classify_timing_path
from matmul_qualification import MATMUL_SPEC
from usage_accounting import UsageLedger

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_REPOSITORY_ROOT = os.path.dirname(_BACKEND_DIR)
# The repository-root .env is the primary full-stack configuration. Keep the
# historical backend/.env as a fallback for variables not defined at root.
# Explicit process exports retain the highest priority (dotenv override=False).
load_dotenv(os.path.join(_REPOSITORY_ROOT, ".env"))
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

logger = logging.getLogger("agent")

SYSTEM_INSTRUCTION = """
You are an autonomous FPGA/ASIC Hardware Design Agent.
Your goal is to fulfill user requests to build specialized hardware accelerators, like MAC units or Neural Network components.
You have tools to lint, simulate, and synthesize Verilog code.

Your mandatory workflow is verification-driven:
1. Analyze the problem and first call define_architecture with an explicit, validated microarchitecture contract. Include target type and technology, clock target (MHz and ns), latency, throughput, pipeline stages, multiplier/DSP strategy, and memory/register strategy. Do not write RTL before the contract is approved. Contract approval is UI-controlled: never ask the user to type "approved" or claim textual approval; tell them to use the Approve & Build button. Once a contract is locked, changes limited to pipeline_stages, multiplier_strategy, memory_strategy, and rationale are internal optimization choices: call define_architecture to record them and continue without reopening approval. Changes to top_module, technology, clock target/period, latency, or throughput are external/material and must pause for a new GenUI approval.
2. Write the Verilog design so it is consistent with the approved contract.
3. Check its syntax with write_and_check_syntax and fix any errors.
4. Write a self-checking testbench (print PASS/FAIL per test, end with $finish) and run it with run_simulation. Do NOT skip this step — a design that has not passed simulation is unproven.
5. Run run_verification with the DesignSpec when a requirement-level signoff is requested. Only a result whose explicit gate.allowed is true authorizes downstream synthesis/delivery; failed or inconclusive mandatory gates block signoff.
6. If the user requests Tiny Tapeout preparation, create a wrapper satisfying the tt_um_<top_module> contract and run run_tiny_tapeout_hardening. Treat an inconclusive or failed result as blocked; even a passed result is local/provider hardening evidence, never foundry signoff.
7. Only after the simulation passes, synthesize with run_silicon_compiler. Use the approved contract clock_period_ns for clocked designs; purely combinational designs still require an explicit contract.
8. For optimization, treat run_silicon_compiler with flow="syn-sta" as the cheap screen. Wait for the controller's candidate-promotion result before requesting flow="full-pnr"; full P&R is authorized only for that exact promoted, simulation-verified RTL and must not be repeated.
9. If simulation or synthesis fails or timing is not met, analyze the output, fix the design consistently with the contract, and repeat the loop from step 3. For an internal optimization, keep the last-good checkpoint and baseline while updating only the allowed internal fields. If an external/material architecture field must change, call define_architecture again first; that proposal requires GenUI approval and previous RTL checkpoints then become ineligible.

Frequency guidance for sky130 designs: a conservative, low-risk target is 40–66 MHz (15–25 ns period). A 10 ns period (100 MHz) is aggressive and should only be attempted for small designs well under 1k gates. Never target a period below 8 ns (125 MHz) in sky130. Do not guess the real Fmax — after the design simulates and synthesizes cleanly, call the find_fmax tool to run a synthesis+STA search and report the actual maximum frequency.

Timing closure: for skywater130 targets at ≥50 MHz, pipeline long combinational datapaths by default (e.g. register the product in a multiply-accumulate PE before the accumulation stage). When synthesis/STA reports a setup violation, identify the longest combinational chain in the failing path, split it with a pipeline register (updating control/schedule logic and the testbench expectations accordingly), re-verify with the same testbench, then re-measure. Never claim timing closure without a measured slack from run_silicon_compiler or find_fmax.

Repair budgets are enforced by the tool loop, per session: at most 5 failed syntax checks and 10 failed tool attempts in total. When a budget is exhausted the tool result says so — stop retrying the same approach, change strategy, and explain the new plan (including the diff of attempted changes) in your next reply. Tool results also end with a token-usage line.

Code style requirements for every Verilog file you produce:
- Start each file with a header comment block: module name, purpose, a port table (name, direction, width, function), clock/reset behavior, and expected behavior (including edge cases like wraparound or saturation).
- Comment each always/assign block with the intent of the logic, not a restatement of the code.
- Use meaningful signal names and comment non-obvious parameters.

Report the final PPA metrics (area, power, slack) and the verification status.
""".strip()

MAX_TOOL_ROUNDS = 14


def _env_int(name: str, default: int) -> int:
    """Read a positive integer override without making startup fragile."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# Model-facing context is deliberately separate from the durable project
# history. Projects retain the complete transcript (including full tool-call
# arguments and tool output), while each completion receives only the current
# request, a compact run ledger, the latest source artifacts, and the protocol
# messages needed to continue an in-flight tool round. These are character
# ceilings rather than token ceilings so this remains provider-independent.
MODEL_CONTEXT_MAX_CHARS = _env_int("AGENT_MODEL_CONTEXT_MAX_CHARS", 120_000)
MODEL_CONTEXT_ARTIFACT_MAX_CHARS = _env_int(
    "AGENT_MODEL_CONTEXT_ARTIFACT_MAX_CHARS", 70_000)
MODEL_CONTEXT_DIAGNOSTIC_MAX_CHARS = _env_int(
    "AGENT_MODEL_CONTEXT_DIAGNOSTIC_MAX_CHARS", 14_000)
MODEL_CONTEXT_RECENT_MESSAGES = _env_int("AGENT_MODEL_CONTEXT_RECENT_MESSAGES", 6)
MODEL_CONTEXT_USER_MAX_CHARS = _env_int("AGENT_MODEL_CONTEXT_USER_MAX_CHARS", 24_000)


# Hard run-level ceilings enforced in code rather than left to the model.
CHAT_TOKEN_BUDGET = _env_int("AGENT_CHAT_TOKEN_BUDGET", 120_000)
# Sentinel distinguishing "field absent" from "explicitly cleared" on the
# project PATCH path (None means clear).
UNSET = object()
# Reserve room for the provider's completion before admitting a paid request.
# This is a ceiling, not a promise that the provider will consume it.
MODEL_OUTPUT_TOKEN_LIMIT = _env_int("AGENT_MODEL_OUTPUT_TOKEN_LIMIT", 32_768)
# Reasoning models (Kimi K3, Mistral GLM 5.3) bill their private thinking
# against the same completion budget as the answer. A 32k cap sized for plain
# models is regularly exhausted by thinking alone: the provider then stops
# with finish_reason=length and an empty response (no text, no tool call).
# Reasoning-capable sessions therefore get a larger per-request ceiling —
# still bounded below by the cumulative run budget in _context_preflight.
REASONING_OUTPUT_TOKEN_LIMIT = _env_int("AGENT_REASONING_OUTPUT_TOKEN_LIMIT", 65_536)
# Context admission is separate from the cumulative per-run budget.
CONTEXT_SOFT_LIMIT = _env_int("AGENT_CONTEXT_SOFT_LIMIT", 180_000)
CONTEXT_HARD_LIMIT = _env_int("AGENT_CONTEXT_HARD_LIMIT", 200_000)
TOKEN_ESTIMATE_BYTES_PER_TOKEN = _env_int("AGENT_TOKEN_ESTIMATE_BYTES_PER_TOKEN", 4)
if CONTEXT_HARD_LIMIT < CONTEXT_SOFT_LIMIT:
    CONTEXT_HARD_LIMIT = CONTEXT_SOFT_LIMIT
MAX_TIMING_VIOLATIONS = _env_int("AGENT_MAX_TIMING_VIOLATIONS", 2)
# Legacy spelling retained for the cheap-screen limit. Full P&R has its own
# independent ceiling so all screen slots can be used before implementation.
MAX_SYNTHESIS_INVOCATIONS = _env_int("AGENT_MAX_SYNTHESIS_INVOCATIONS", 3)
MAX_SCREEN_INVOCATIONS = _env_int(
    "AGENT_MAX_SCREEN_INVOCATIONS", MAX_SYNTHESIS_INVOCATIONS)
# A synthesis result that is no better than the current verified checkpoint is
# useful evidence, but blindly continuing to generate another rewrite is not.
# Keep this separate from the synthesis invocation ceiling: a genuinely better
# revision gets a fresh chance even when an earlier attempt was a regression.
MAX_NON_IMPROVING_ATTEMPTS = _env_int("AGENT_MAX_NON_IMPROVING_ATTEMPTS", 2)
# Invalid preflights do not consume EDA invocation budgets, but still need a
# hard loop bound so a provider cannot repeat an impossible request forever.
MAX_TIMING_ADMISSION_REJECTIONS = _env_int(
    "AGENT_MAX_TIMING_ADMISSION_REJECTIONS", 3)
# Phase 3 optimization admission. Synthesis/STA is an inexpensive screen;
# place-and-route is a scarce confirmation step and must only run for a
# candidate that the screen promoted. Keep this policy in the agent layer so
# a model cannot accidentally turn an exploratory rewrite into an expensive
# implementation experiment.
MAX_FULL_PNR_INVOCATIONS = _env_int("AGENT_MAX_FULL_PNR_INVOCATIONS", 1)
_SCREEN_FLOW_NAMES = frozenset({
    "", "auto", "screen", "cheap", "cheap-screen", "cheap_screen",
    "syn-sta", "syn_sta", "synthesis", "synthesis-sta", "synthesis_sta",
    "fmax", "timing",
})
_FULL_PNR_FLOW_NAMES = frozenset({
    "full", "pnr", "full-pnr", "full_pnr", "place-and-route",
    "place_and_route", "implementation", "asic", "signoff",
})

# Streaming ceilings are deliberately separate from the provider's HTTP
# timeout. A long design response may legitimately take minutes, but a
# provider that has not sent the first chunk (or has gone silent between
# chunks) must not leave the UI in an indistinguishable infinite wait.
LLM_TIMEOUT_S = 900.0
STREAM_FIRST_TOKEN_TIMEOUT_S = 120.0
STREAM_INACTIVITY_TIMEOUT_S = 120.0
# A stream that has started is bounded by first-token and inactivity
# deadlines, not by an arbitrary wall-clock ceiling. Long provider reasoning
# can legitimately exceed several minutes; active chunks keep the stream live.
# Cancellation, first-token/inactivity deadlines, context, and tool-round
# budgets remain the bounded controls.
STREAM_OVERALL_TIMEOUT_S = 0.0

# Before the first streamed chunk, a status event is emitted at this interval.
# Once chunks arrive the same cadence is used only while a provider is silent;
# each received chunk resets the inactivity deadline. Monkeypatched to a
# fraction of a second in tests.
STATUS_HEARTBEAT_S = 15.0

# OpenAI function-tool schemas matching the signatures in eda_tools.py
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "define_architecture",
            "description": "First step for a hardware design. Validates and approves the machine-readable architecture contract before any RTL is generated: target_type (fpga or asic), technology, clock target in MHz and ns, latency/throughput, pipeline stages, multiplier/DSP strategy, and memory/register strategy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_type": {"type": "string", "enum": ["fpga", "asic"]},
                    "top_module": {"type": "string", "description": "Portable top-level RTL module name."},
                    "technology": {"type": "string", "description": "Target FPGA family/board or ASIC process/library, e.g. ice40 or sky130."},
                    "clock_target_mhz": {"type": "number", "exclusiveMinimum": 0},
                    "clock_period_ns": {"type": "number", "exclusiveMinimum": 0, "description": "1000 / clock_target_mhz; the STA contract."},
                    "latency_cycles": {"type": "integer", "minimum": 1},
                    "throughput_per_cycle": {"type": "number", "exclusiveMinimum": 0},
                    "pipeline_stages": {"type": "integer", "minimum": 0},
                    "multiplier_strategy": {"type": "string", "enum": ["inferred", "dsp", "fabric", "macro", "none"]},
                    "memory_strategy": {"type": "string", "enum": ["registers", "bram", "sram", "distributed_ram", "external", "none"]},
                    "rationale": {"type": "string"},
                },
                "required": ["target_type", "top_module", "technology", "clock_target_mhz", "clock_period_ns", "latency_cycles", "throughput_per_cycle", "pipeline_stages", "multiplier_strategy", "memory_strategy"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_and_check_syntax",
            "description": "Writes Verilog code to disk and lints it with an installed HDL linter (slang, verilator, iverilog, or yosys). Returns a structured JSON report: ok, verdict (ok | lint_warning | syntax_error | elaboration_error), error/warning totals, and up to 8 diagnostics with file/line/col/code and caret snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "verilog_code": {"type": "string", "description": "Complete Verilog source code."},
                    "top_module": {"type": "string", "description": "Name of the top-level module."},
                },
                "required": ["verilog_code", "top_module"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_simulation",
            "description": "Runs a functional simulation of the Verilog design with a self-checking testbench (iverilog or verilator). Use this to verify the design works before synthesis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "verilog_code": {"type": "string", "description": "Complete Verilog source code of the design."},
                    "testbench_code": {"type": "string", "description": "Self-checking testbench: print PASS/FAIL per test and end with $finish."},
                    "top_module": {"type": "string", "description": "Name of the top-level design module."},
                },
                "required": ["verilog_code", "testbench_code", "top_module"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_verification",
            "description": "Runs the unified verification pipeline as a JobManager job: lint, independent differential regression, assertions, coverage, formal properties, RTL equivalence, CDC/RDC, and optional Icarus portability. Returns a requirement matrix, RunManifest/EvidenceReport, and an explicit gate.allowed decision. Failed or inconclusive mandatory gates do not authorize synthesis or delivery.",
            "parameters": {
                "type": "object",
                "properties": {
                    "verilog_code": {"type": "string", "description": "Complete Verilog/SystemVerilog source code."},
                    "testbench_code": {"type": "string", "description": "Self-checking testbench source code."},
                    "top_module": {"type": "string", "description": "Name of the top-level design module."},
                    "design_spec": {"type": "object", "description": "Optional DesignSpec JSON. Include requirements, verification assertions/coverage, and metadata.reference_model for differential regression."},
                    "run_icarus": {"type": "boolean", "description": "Also run the optional Icarus portability smoke test.", "default": False},
                    "reference_rtl_code": {"type": "string", "description": "Optional trusted RTL required when verification.requires_equivalence is true."},
                },
                "required": ["verilog_code", "testbench_code", "top_module"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tiny_tapeout_hardening",
            "description": "Runs the conservative Tiny Tapeout Sky130 hardening flow as a cancellable JobManager job. Returns content-addressed inputs, capabilities, streamed job output, and an explicit passed/failed/inconclusive result. A local pass is open-source hardening evidence, never foundry signoff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wrapper_verilog": {"type": "string", "description": "Complete Tiny Tapeout wrapper Verilog declaring tt_um_<top_module>."},
                    "top_module": {"type": "string", "description": "Portable user design name; the wrapper top is tt_um_<top_module>."},
                    "title": {"type": "string", "description": "Optional Tiny Tapeout project title."},
                    "author": {"type": "string", "description": "Optional project author."},
                    "description": {"type": "string", "description": "Optional project description."},
                    "clock_hz": {"type": "integer", "description": "Wrapper clock metadata in Hz (default 50000000).", "default": 50000000, "minimum": 1},
                },
                "required": ["wrapper_verilog", "top_module"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_silicon_compiler",
            "description": "Synthesizes Verilog code with SiliconCompiler and returns PPA (area, power, slack) metrics or errors.",
            "parameters": {
                "type": "object",
                "properties": {
                    "verilog_code": {"type": "string", "description": "Complete Verilog source code."},
                    "top_module": {"type": "string", "description": "Name of the top-level module."},
                    "target": {"type": "string", "description": "SiliconCompiler target/PDK demo (default skywater130_demo; freepdk45_demo currently has a flowgraph issue).", "default": "skywater130_demo"},
                    "clock_period_ns": {"type": "number", "description": "Clock period in nanoseconds for designs with a 'clk' port. Generates an SDC constraint (create_clock) so timing is analyzed and slack metrics are reported. If omitted on a design with a 'clk' input, a 10 ns default is applied automatically; pass an explicit value to set a real frequency target."},
                    "constraints": {"type": "object", "description": "Optional typed ConstraintSet contract (clocks, I/O delays, uncertainties, false paths, and multicycle paths). It is rendered to one authoritative SDC and consumed byte-for-byte; do not pass this together with clock_period_ns."},
                    "flow": {"type": "string", "enum": ["syn-sta", "full-pnr"], "description": "Implementation stage. Use syn-sta for the isolated cheap synthesis+STA candidate screen (default); full-pnr is admitted only after the controller promotes this exact simulation-verified RTL and fails closed if P&R tools are unavailable."},
                    "promotion_token": {"type": "string", "description": "Opaque token returned by a promoted cheap screen; pass it unchanged only when requesting full-pnr."},
                    "timing_intent": {"type": "object", "description": "Required on a retry after measured negative timing slack. State strategy, rationale, expected_change, and target_startpoint/target_endpoint when a critical path is available (or evidence when no path is available).", "properties": {"strategy": {"type": "string"}, "rationale": {"type": "string"}, "expected_change": {"type": "string"}, "target_startpoint": {"type": "string"}, "target_endpoint": {"type": "string"}, "evidence": {"type": "string"}, "latency_cycles": {"type": "integer", "minimum": 1, "description": "Must preserve the approved architecture latency."}, "throughput_per_cycle": {"type": "number", "exclusiveMinimum": 0, "description": "Must preserve the approved architecture throughput."}}, "additionalProperties": False},
                },
                "required": ["verilog_code", "top_module"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_fmax",
            "description": "Runs synthesis + STA and performs a bounded coarse-to-fine maximum-clock search with confirmation. Returns structured observations, timing bounds, uncertainty and assumptions. This is engineering evidence, not signoff STA.",
            "parameters": {
                "type": "object",
                "properties": {
                    "verilog_code": {"type": "string", "description": "Complete Verilog source code."},
                    "top_module": {"type": "string", "description": "Name of the top-level module."},
                    "target": {"type": "string", "description": "SiliconCompiler target/PDK demo (default skywater130_demo).", "default": "skywater130_demo"},
                    "period_tolerance_ns": {"type": "number", "description": "Refinement tolerance in ns (default 0.5).", "default": 0.5},
                    "max_runs": {"type": "integer", "description": "Hard probe budget including confirmations (default 12).", "default": 12},
                    "confirm_runs": {"type": "integer", "description": "Repeated probes at the selected period (default 1).", "default": 1},
                    "min_period_ns": {"type": "number", "description": "Fast search-bracket edge in ns (default 5).", "default": 5.0},
                    "max_period_ns": {"type": "number", "description": "Slow search-bracket edge in ns (default 40).", "default": 40.0},
                    "constraints": {"type": "object", "description": "Optional typed ConstraintSet contract. Fmax requires exactly one clock; each probe receives a period-adjusted authoritative SDC."},
                    "timing_intent": {"type": "object", "description": "Required on a retry after measured negative timing slack; declare strategy, rationale, expected_change, and path targets or evidence.", "properties": {"strategy": {"type": "string"}, "rationale": {"type": "string"}, "expected_change": {"type": "string"}, "target_startpoint": {"type": "string"}, "target_endpoint": {"type": "string"}, "evidence": {"type": "string"}, "latency_cycles": {"type": "integer", "minimum": 1, "description": "Must preserve the approved architecture latency."}, "throughput_per_cycle": {"type": "number", "exclusiveMinimum": 0, "description": "Must preserve the approved architecture throughput."}}, "additionalProperties": False},
                },
                "required": ["verilog_code", "top_module"],
            },
        },
    },
]

_TOOL_FUNCS = {
    "define_architecture": define_architecture,
    "write_and_check_syntax": write_and_check_syntax,
    "run_simulation": run_simulation,
    "run_verification": run_verification,
    "run_tiny_tapeout_hardening": run_tiny_tapeout_hardening,
    "run_silicon_compiler": run_silicon_compiler,
    "find_fmax": find_fmax,
}

# Per-connection chat histories, keyed by an arbitrary session id.
_histories: dict[str, list[dict]] = {}

# Public run progress is deliberately separate from the model transcript. It
# is a small, durable workbench ledger: concise enough to replay after a
# refresh, but never fed back into the provider context as chat history.
PUBLIC_TIMELINE_MAX = 100
_project_timelines: dict[str, list[dict]] = {}

# Per-session repair-policy state (Task 5), in memory only: syntax repair
# attempts (cap SYNTAX_REPAIR_CAP), total failed tool attempts (cap
# TOTAL_REPAIR_CAP), the last sim-passing Verilog snapshot, and accumulated
# token usage. See _postprocess_tool_result.
SYNTAX_REPAIR_CAP = 5
TOTAL_REPAIR_CAP = 10
# A provider can exhaust its completion budget after emitting only private
# reasoning. One automatic continuation is safe and useful; allowing more
# would turn a provider-side length stop into an unbounded paid loop.
MAX_EMPTY_CONTINUATIONS = 1

_session_state: dict[str, dict] = {}


def _get_state(session_id: str) -> dict:
    return _session_state.setdefault(session_id, {
        "syntax_attempts": 0,      # failed write_and_check_syntax calls
        "repair_attempts": 0,      # failed tool calls of any kind
        "last_good_verilog": None,  # last design that passed simulation
        "prev_lint_code": None,    # verilog_code of the previous lint call
        "tokens_used": 0,          # accumulated total_tokens for the session
        # API usage counters; prompt estimates never increment these.
        "api_prompt_tokens": 0,
        "api_completion_tokens": 0,
        "api_total_tokens": 0,
        "usage_ledger": UsageLedger().snapshot(),
        "tokens_remaining": None,  # remaining budget if the API reports one
        "chat_token_start": 0,    # cumulative usage at this chat run start
        "next_prompt_estimate": None,  # deterministic estimate, not API usage
        "context_warning_emitted": False,
        "timing_violations": 0,      # negative setup-slack synthesis results
        "timing_diagnosis": None,
        "timing_retry_history": [],  # accepted strategies for this run
        "timing_diagnostic_measurements": set(),
        "timing_admission_rejections": 0,
        "synthesis_invocations": 0,  # synthesis/find_fmax calls this chat
        "seen_tool_calls": {},       # canonical call -> (result, failed)
        "run_stop_reason": None,     # explicit fail-closed stop message
        "controller_run_id": None,   # identity only; durable lifecycle is S3
        "run_snapshot": None,        # versioned authoritative durable run state
        # Verified RTL checkpoints and the best synthesis result for this chat
        # session. The code is retained so a failed optimization can point the
        # model back to a known-good revision.
        "revisions": [],             # ordered list of revision snapshots
        "revision_by_hash": {},      # sha256 -> snapshot (same object)
        "best_revision": None,       # snapshot selected by _revision_score
        "non_improving_attempts": 0,
        "active_revision": None,
        "latest_attempted_revision": None,
        "setupslack_ns": None,
        # Phase 3: cheap-screen promotion and bounded full P&R confirmation.
        # The baseline is retained while a promoted candidate is confirmed so
        # a failed implementation can never displace the last good checkpoint.
        "screen_attempts": 0,
        "screen_passes": 0,
        "screen_best_revision": None,
        "promoted_revision": None,
        "promotion_baseline": None,
        "full_pnr_invocations": 0,
        "full_pnr_by_hash": {},
        "full_pnr_active_hash": None,
        "full_pnr_result": None,
        # Public run-ledger counters. These are separate from
        # repair_attempts: the former counts calls shown to the user.
        "tool_attempts": 0,
        "current_round": 0,
        "planned_hashes": set(),
        # Phase 1 architecture-first admission.  The contract is approved by
        # define_architecture and retained across chat runs/project reloads.
        "architecture_contract": None,
        "architecture_contract_hash": None,
        "architecture_required": False,
        "design_contract": None,
        "contract_revision_diff": None,
        # Phase 3 contract controller: the original request is retained while
        # the user reviews the proposal; it is consumed only by approve-and-build.
        "pending_request": None,
        "contract_run_revision": None,
        "contract_run_digest": None,
        "contract_gate_pending": False,
        # The current user request is transient run context. It lets a
        # proposal raised during an approve-and-build continuation retain the
        # original request for the next explicit approval gate.
        "current_request": None,
        # Sticky qualification admission. Once a substantive request names the
        # frozen benchmark, terse continuation/approval turns must not weaken
        # its contract by replacing the request with generic wording.
        "benchmark_contract_id": None,
        # Bounded recovery for reasoning-only provider length stops. The
        # continuation instruction is transient and is not shown as chat.
        "empty_continuations": 0,
        "stale_approval_continuations": 0,
        "continuation_instruction": None,
        "continuation_kwargs": None,
    })


def _architecture_request_required(message: str) -> bool:
    """Identify messages that ask the agent to create or change hardware.

    Small utility chats/tests that only lint an already supplied snippet remain
    compatible; real design requests enter the strict architecture gate.
    """
    return bool(re.search(
        r"\b(build|design|generate|implement|write|rtl|verilog|hardware|"
        r"accelerator|processor|fpga|asic|matmul|counter|risc[- ]?v)\b",
        str(message or ""), re.IGNORECASE))


def _is_retry_intent(message: str) -> bool:
    """Return whether a turn is the deliberately terse recovery command."""
    return bool(re.fullmatch(
        r"\s*(?:retry|continue|resume|try again)\s*[.!]?\s*",
        str(message or ""), re.IGNORECASE))


def _implicit_contract_resume(message: str, effective_request: str, state: dict) -> bool:
    """Allow only terse retry of a durable locked architecture request."""
    if not _is_retry_intent(message) or not _architecture_request_required(effective_request):
        return False
    if state.get("pending_request") is not None:
        return False
    contract = state.get("design_contract")
    if not isinstance(contract, dict):
        return False
    try:
        return _contract_state_from(contract).status == "locked"
    except ContractProtocolError:
        return False


def _stale_approval_request(text: str, state: dict) -> bool:
    """Detect model prose requesting approval when no approval gate exists."""
    if state.get("contract_gate_pending"):
        return False
    contract = state.get("design_contract")
    if not isinstance(contract, dict):
        return False
    try:
        if _contract_state_from(contract).status not in {"locked", "approved"}:
            return False
    except ContractProtocolError:
        return False
    return bool(re.search(
        r"(?:ready\s+for\s+approval|await(?:ing)?\s+(?:your\s+)?approval|"
        r"(?:use|click)\s+(?:the\s+)?(?:\*\*)?approve\s*(?:&|and)\s*build|"
        r"please\s+reply\s+[\"']?approved)",
        str(text or ""), re.IGNORECASE,
    ))


def _effective_request(message: str, history: list[dict]) -> str:
    """Resolve terse retry turns to the latest substantive design request."""
    if not _is_retry_intent(message):
        return message
    for entry in reversed(history or []):
        prior = entry.get("content") if entry.get("role") == "user" else None
        if isinstance(prior, str) and _architecture_request_required(prior):
            return prior
    return message


def _is_frozen_matmul3_request(message: str | None) -> bool:
    """Return whether the request explicitly names the frozen matmul3 benchmark.

    Generic matrix-multiply requests are intentionally not treated as the
    qualification benchmark. Only the explicit benchmark identifier opts a
    run into the immutable Phase 3 contract below.
    """
    return bool(re.search(r"\bmatmul3\b", str(message or ""), re.IGNORECASE))


def _benchmark_contract_id_for(state: dict | None, message: str | None = None) -> str | None:
    """Return the durable benchmark admission selected by a substantive turn."""
    state = state or {}
    benchmark_id = state.get("benchmark_contract_id")
    if benchmark_id == "matmul3":
        return benchmark_id
    if _is_frozen_matmul3_request(message):
        return "matmul3"
    return None


def _history_selects_frozen_matmul3(history: list[dict] | None) -> bool:
    """Backfill sticky qualification admission from old project transcripts."""
    return any(
        entry.get("role") == "user" and _is_frozen_matmul3_request(entry.get("content"))
        for entry in (history or [])
    )


def _latest_verified_frozen_matmul3_architecture(
        history: list[dict] | None) -> ArchitectureContract | None:
    """Return the latest canonical architecture followed by a passing sim.

    This is used only to repair projects written by older controllers. A mere
    proposal is insufficient: later simulation evidence proves that the gate
    had admitted and executed that architecture.
    """
    candidate: ArchitectureContract | None = None
    verified: ArchitectureContract | None = None
    calls: dict[str, str] = {}
    for entry in history or []:
        if entry.get("role") == "assistant":
            for call in entry.get("tool_calls") or []:
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                call_id = str(call.get("id") or "")
                if call_id:
                    calls[call_id] = name
                if name != "define_architecture":
                    continue
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    parsed = ArchitectureContract.model_validate(args)
                except Exception:
                    continue
                if not _frozen_matmul3_admission_errors(
                        {"contract": parsed.canonical()}, None,
                        benchmark_contract_id="matmul3"):
                    candidate = parsed
        elif (entry.get("role") == "tool" and candidate is not None and
              calls.get(str(entry.get("tool_call_id") or "")) == "run_simulation" and
              "SIMULATION PASSED" in str(entry.get("content") or "")):
            verified = candidate
    return verified


def _frozen_matmul3_context(message: str | None, *, benchmark_contract_id: str | None = None) -> str:
    """Render the canonical frozen benchmark contract for the model prompt."""
    if benchmark_contract_id != "matmul3" and not _is_frozen_matmul3_request(message):
        return ""
    canonical = json.dumps(MATMUL_SPEC.canonical(), sort_keys=True, indent=2,
                           ensure_ascii=False)
    throughput = 1.0 / MATMUL_SPEC.initiation_interval_cycles
    return (
        "FROZEN BENCHMARK CONTRACT — matmul3 (sole source of truth)\n"
        "The current request explicitly names the frozen matmul3 benchmark. "
        "Do not reconstruct or reinterpret its interface or behavior. The "
        "following canonical object is authoritative:\n"
        f"{canonical}\n"
        "When calling define_architecture, use top_module, technology, clock "
        "frequency/period, and latency exactly as above. Map the frozen "
        "initiation_interval_cycles to throughput_per_cycle as reciprocal: "
        f"throughput_per_cycle = 1 / {MATMUL_SPEC.initiation_interval_cycles} "
        f"= {throughput:g}. Do not substitute a different port list, reset, "
        "arithmetic definition, latency, initiation interval, or target."
    )


def _frozen_matmul3_admission_errors(payload: dict, message: str | None,
                                     *, benchmark_contract_id: str | None = None) -> list[str]:
    """Reject architecture fields that contradict the frozen matmul3 spec.

    This is deliberately applied only while admitting a proposal for an
    explicitly named matmul3 request. The generic architecture validator
    remains technology-neutral for all other projects.
    """
    if benchmark_contract_id != "matmul3" and not _is_frozen_matmul3_request(message):
        return []
    # define_architecture returns the candidate under ``contract``; accepting
    # the outer result here would compare every field against ``None``.
    candidate = payload.get("contract") if isinstance(payload.get("contract"), dict) else payload
    errors: list[str] = []
    expected_top = MATMUL_SPEC.top_module
    if candidate.get("top_module") != expected_top:
        errors.append(f"top_module must be '{expected_top}' for frozen matmul3")
    technology = str(candidate.get("technology") or "").strip().lower()
    if technology != MATMUL_SPEC.technology.lower():
        errors.append(f"technology must be '{MATMUL_SPEC.technology}' for frozen matmul3")
    if candidate.get("target_type") != "asic":
        errors.append("target_type must be 'asic' for frozen matmul3")

    def _number(name: str) -> float | None:
        try:
            value = float(candidate.get(name))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    frequency = _number("clock_target_mhz")
    if frequency is None or not math.isclose(
        frequency, MATMUL_SPEC.target_fmax_mhz, rel_tol=2e-3, abs_tol=1e-3
    ):
        errors.append(
            f"clock_target_mhz must be {MATMUL_SPEC.target_fmax_mhz:g} for frozen matmul3"
        )
    period = _number("clock_period_ns")
    if period is None or not math.isclose(
        period, MATMUL_SPEC.target_period_ns, rel_tol=2e-3, abs_tol=1e-3
    ):
        errors.append(
            f"clock_period_ns must be {MATMUL_SPEC.target_period_ns:g} ns for frozen matmul3"
        )
    if candidate.get("latency_cycles") != MATMUL_SPEC.latency_cycles:
        errors.append(
            f"latency_cycles must be {MATMUL_SPEC.latency_cycles} for frozen matmul3"
        )
    throughput = _number("throughput_per_cycle")
    expected_throughput = 1.0 / MATMUL_SPEC.initiation_interval_cycles
    if throughput is None or not math.isclose(
        throughput, expected_throughput, rel_tol=2e-3, abs_tol=1e-6
    ):
        errors.append(
            "throughput_per_cycle must equal the reciprocal of frozen "
            f"initiation_interval_cycles ({expected_throughput:g})"
        )
    return errors


def _architecture_contract_for(state: dict) -> ArchitectureContract | None:
    payload = state.get("architecture_contract")
    if not isinstance(payload, dict):
        return None
    try:
        contract = ArchitectureContract.model_validate(payload)
        expected = _architecture_contract_hash(contract)
        if state.get("architecture_contract_hash") != expected:
            return None
        return contract
    except Exception:
        return None

# Project persistence: sessions whose id starts with "proj-" are persisted to
# PROJECTS_DIR/<session_id>.json ({"name": ..., "history": [...], plus an
# optional "model" registry key — absent means the registry default).
PROJECTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")

_project_repository: ProjectRepository | None = None


def _projects() -> ProjectRepository:
    """Return the repository for the current runtime project directory.

    ``PROJECTS_DIR`` remains a module-level setting for deployment and tests;
    refresh the repository when it changes (for example, to a test directory).
    """
    global _project_repository
    configured = os.path.abspath(PROJECTS_DIR)
    if (_project_repository is None or
            str(_project_repository.directory) != configured):
        _project_repository = ProjectRepository(PROJECTS_DIR)
    return _project_repository


def _project_lock(session_id: str):
    return _projects().lock(session_id)


def _project_path(session_id: str) -> str | None:
    path = _projects().path(session_id)
    return str(path) if path else None


_TIMELINE_TYPES = {"progress", "decision", "phase", "timeline", "run_progress",
                   "run_status", "structured_progress", "structured_status",
                   "decision_status"}
_TIMELINE_PRIVATE_KEYS = {"analysis", "chain_of_thought", "cot", "inner_monologue",
                          "thought", "thoughts"}
_TIMELINE_FIELDS = {"phase", "action", "status", "event_id", "id", "timestamp",
                    "reason", "evidence", "attempt", "attempts", "attempt_count",
                    "metric", "metrics", "delta", "metric_delta", "budgets", "token_budget",
                    "tokens_remaining", "tokens_used", "tool", "tool_name", "name",
                    "job_id", "jobs", "candidate_ref", "best_ref", "terminal_reason",
                    "approval_pause", "lifecycle"}


_TIMELINE_NESTED_FIELDS = {"attempts", "metrics", "delta", "metric_delta", "budgets"}
_TIMELINE_NESTED_MAX_DEPTH = 3
_TIMELINE_NESTED_MAX_ENTRIES = 16


def _sanitize_timeline_nested(value: object, depth: int = 0):
    """Bound public telemetry trees while preserving scalar/numeric values."""
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, str):
        return _truncate(value, 160)
    if depth >= _TIMELINE_NESTED_MAX_DEPTH:
        return None
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:_TIMELINE_NESTED_MAX_ENTRIES]:
            key = str(key)
            if key in _TIMELINE_PRIVATE_KEYS:
                continue
            clean = _sanitize_timeline_nested(item, depth + 1)
            if clean is not None:
                result[key[:80]] = clean
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in list(value)[:_TIMELINE_NESTED_MAX_ENTRIES]:
            clean = _sanitize_timeline_nested(item, depth + 1)
            if clean is not None:
                result.append(clean)
        return result
    return None

def _sanitize_timeline_event(event: object) -> dict | None:
    """Keep only concise public progress fields for durable UI replay."""
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "progress").lower()
    if event_type not in _TIMELINE_TYPES:
        return None
    content = event.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(content, dict):
        return None
    safe = {}
    for key, value in content.items():
        if key in _TIMELINE_PRIVATE_KEYS or key not in _TIMELINE_FIELDS:
            continue
        if key in _TIMELINE_NESTED_FIELDS:
            clean = _sanitize_timeline_nested(value)
            if clean is not None:
                safe[key] = clean
        elif isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, float) and not math.isfinite(value):
                continue
            safe[key] = _truncate(str(value), 320) if isinstance(value, str) else value
    if not any(safe.get(key) for key in ("phase", "action", "reason", "evidence")):
        return None
    event_id = safe.get("event_id") or safe.get("id") or event.get("event_id")
    if not event_id:
        event_id = hashlib.sha256(
            json.dumps([event_type, safe], sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]
    safe["event_id"] = _truncate(str(event_id), 120)
    return {"type": event_type, "content": safe}


def _sanitize_timeline_list(events: object) -> list[dict]:
    if not isinstance(events, list):
        return []
    result = []
    positions = {}
    for event in events:
        clean = _sanitize_timeline_event(event)
        if clean is None:
            continue
        event_id = clean["content"]["event_id"]
        if event_id in positions:
            result[positions[event_id]] = clean
        else:
            positions[event_id] = len(result)
            result.append(clean)
    return result[-PUBLIC_TIMELINE_MAX:]


_RUN_STATE_VERSION = 1
_RUN_TERMINAL = {"completed", "failed", "cancelled", "interrupted", "inconclusive"}


def _run_lifecycle(status: object, reason: object = "", *, approval: bool = False) -> str:
    text = str(reason or "").lower()
    if approval or "approval" in text or "answer_required" in text:
        return "awaiting approval"
    value = str(status or "running").lower()
    if value in {"failed", "blocked", "error"}:
        return "inconclusive" if "inconclusive" in text or "diagnosis" in text else "failed"
    if value in _RUN_TERMINAL:
        return value
    return "running"


def _run_snapshot_for(session_id: str, *, event: dict | None = None,
                      lifecycle: str | None = None, terminal_reason: str | None = None,
                      increment: bool = False) -> dict:
    """Build the one authoritative, bounded run snapshot stored in project JSON."""
    st = _get_state(session_id)
    previous = st.get("run_snapshot") if isinstance(st.get("run_snapshot"), dict) else {}
    content = (event or {}).get("content") if isinstance(event, dict) else {}
    content = content if isinstance(content, dict) else {}
    run_id = st.get("controller_run_id") or previous.get("run_id")
    seq = int(previous.get("seq", 0) or 0) + (1 if increment else 0)
    def ref(value):
        if not isinstance(value, dict):
            return None
        return {key: value[key] for key in ("revision", "sha256", "candidate_id", "top_module") if value.get(key) is not None}
    reason = terminal_reason or content.get("terminal_reason") or content.get("reason")
    approval = bool(st.get("contract_gate_pending") or st.get("pending_request"))
    status = lifecycle or _run_lifecycle(content.get("status"), reason, approval=approval)
    jobs = content.get("jobs")
    if jobs is None and content.get("job_id"):
        jobs = [content["job_id"]]
    if not isinstance(jobs, list):
        jobs = previous.get("jobs") if isinstance(previous.get("jobs"), list) else []
    snapshot = {
        "version": _RUN_STATE_VERSION, "run_id": run_id, "seq": seq,
        "lifecycle": status, "phase": content.get("phase") or previous.get("phase"),
        "attempt": content.get("attempts") or content.get("attempt") or previous.get("attempt"),
        "candidate_ref": ref(st.get("latest_attempted_revision") or st.get("active_revision")) or content.get("candidate_ref") or previous.get("candidate_ref"),
        "best_ref": ref(st.get("best_revision")) or content.get("best_ref") or previous.get("best_ref"),
        "budgets": content.get("budgets") or _event_budgets(st), "jobs": jobs[-32:],
        "approval_pause": ({"pending": True, "reason": "awaiting_contract_approval"} if approval else None),
        "last_decision": (content if isinstance(event, dict) and event.get("type") == "decision" else previous.get("last_decision")),
        "terminal_reason": reason if status in _RUN_TERMINAL or reason else previous.get("terminal_reason"),
    }
    snapshot = _sanitize_timeline_nested(snapshot)
    st["run_snapshot"] = snapshot
    return snapshot


def record_public_timeline_event(session_id: str, event: dict) -> bool:
    """Persist one public structured event without adding it to model history."""
    if not isinstance(session_id, str) or not session_id.startswith("proj-"):
        return False
    clean = _sanitize_timeline_event(event)
    if clean is None:
        return False
    with _project_lock(session_id):
        _get_history(session_id)
        current = _sanitize_timeline_list(_project_timelines.get(session_id, []))
        event_id = clean["content"]["event_id"]
        existing = next((i for i, item in enumerate(current)
                         if item["content"].get("event_id") == event_id), None)
        if existing is None:
            current.append(clean)
        else:
            current[existing] = clean
        _project_timelines[session_id] = current[-PUBLIC_TIMELINE_MAX:]
        _run_snapshot_for(session_id, event=clean, increment=existing is None)
        _save_project(session_id)
    return True


def _save_project(session_id: str) -> None:
    path = _project_path(session_id)
    if not path:
        return
    # Cover both the in-memory snapshot and the atomic replace.  No model or
    # EDA work occurs while this short critical section is held.
    with _project_lock(session_id):
        if session_id not in _histories:
            return
        # Normalize old projects as they are written back. This also removes
        # provider-private reasoning fields that may have been persisted by a
        # previous backend version.
        cleaned_history = _sanitize_history(_histories[session_id])
        # Preserve the list object held by an in-flight chat generator; replacing
        # it would make subsequent assistant/tool appends invisible to saves.
        _histories[session_id][:] = cleaned_history
        name = _project_names.get(session_id, session_id)
        data = {"name": name, "history": _histories[session_id]}
        model = _project_models.get(session_id)
        if model:
            data["model"] = model
        effort = _project_efforts.get(session_id)
        if effort:
            data["reasoning_effort"] = effort
        created = _project_created.get(session_id)
        if created:
            data["created"] = created
        timeline = _project_timelines.get(session_id) or []
        if timeline:
            data["timeline"] = timeline[-PUBLIC_TIMELINE_MAX:]
        # Dashboard restore state is derived from history, whose assistant
        # entries normally carry the full tool-call arguments. Keep a prior
        # genuine full snapshot as a fallback if an old compacted transcript
        # has no recoverable source; never persist a marker as dashboard code.
        previous_verilog = None
        previous = None
        try:
            previous = _projects().load(session_id)
            if isinstance(previous, dict):
                previous_verilog = _valid_dashboard_verilog(previous.get("latest_verilog"))
        except CorruptProjectError:
            previous_verilog = None
        verilog, metrics, lint = _derive_latest_state(
            _histories[session_id], fallback=previous_verilog)
        metric_provenance = _derive_latest_metric_provenance(_histories[session_id])
        if verilog:
            data["latest_verilog"] = verilog
        if metrics:
            data["latest_metrics"] = metrics
        if lint:
            data["latest_lint"] = lint
        if metric_provenance:
            data["metric_provenance"] = metric_provenance
        architecture = _get_state(session_id).get("architecture_contract")
        architecture_hash = _get_state(session_id).get("architecture_contract_hash")
        if isinstance(architecture, dict) and isinstance(architecture_hash, str):
            data["architecture_contract"] = architecture
            data["architecture_contract_hash"] = architecture_hash
        design_contract = _get_state(session_id).get("design_contract")
        if isinstance(design_contract, dict):
            data["design_contract"] = design_contract
        revision_diff = _get_state(session_id).get("contract_revision_diff")
        if isinstance(revision_diff, list) and revision_diff:
            data["contract_revision_diff"] = revision_diff
        pending_request = _get_state(session_id).get("pending_request")
        if isinstance(pending_request, str) and pending_request.strip():
            data["pending_request"] = pending_request
        benchmark_contract_id = _get_state(session_id).get("benchmark_contract_id")
        if benchmark_contract_id == "matmul3":
            data["benchmark_contract_id"] = benchmark_contract_id
        lineage = _serialize_candidate_lineage(_get_state(session_id))
        if lineage:
            data["candidate_lineage"] = lineage
        snapshot = _get_state(session_id).get("run_snapshot")
        if isinstance(snapshot, dict):
            data["run_state"] = snapshot
        usage_ledger = _get_state(session_id).get("usage_ledger")
        if isinstance(usage_ledger, dict) and usage_ledger.get("version") == 1:
            data["usage_ledger"] = usage_ledger
        # The active multi-file source set is repository-owned, not derived
        # from the model transcript. Preserve it across later chat saves.
        if isinstance(previous, dict) and previous.get("hardware_project"):
            try:
                data["hardware_project"] = validate_manifest(
                    previous["hardware_project"])
            except HardwareProjectError:
                logger.error("[%s] invalid hardware project manifest", session_id)
        _projects().save(session_id, data)


def get_project_state(session_id: str) -> dict:
    """Read persisted dashboard state; return an empty dict if unavailable."""
    try:
        data = _projects().load(session_id)
    except CorruptProjectError:
        return {}
    if not data:
        return {}
    state = {}
    # ``latest_verilog`` predates artifact handles and may be stale (or may
    # contain a compact marker from an older save). Re-derive dashboard state
    # from the durable transcript on every project join, then use the persisted
    # snapshot only as a fallback when it is a genuine full source blob.
    persisted_verilog = _valid_dashboard_verilog(data.get("latest_verilog"))
    history = _sanitize_history(data.get("history", []))
    derived_verilog, derived_metrics, derived_lint = _derive_latest_state(
        history, fallback=persisted_verilog)
    if derived_lint:
        state["lint"] = derived_lint
    lineage = _validated_candidate_lineage(data.get("candidate_lineage"))
    active = lineage.get("active") if lineage else None
    active_metrics_selected = False
    if isinstance(active, dict):
        state["verilog"] = {"top": active.get("top_module") or "top",
                            "code": active["verilog_code"]}
        if isinstance(active.get("metrics"), dict) and active["metrics"]:
            state["metrics"] = dict(active["metrics"])
            active_metrics_selected = True
        state["candidate_lineage"] = _public_candidate_lineage(lineage)
    elif derived_verilog is not None:
        state["verilog"] = derived_verilog
    persisted_metrics = data.get("latest_metrics")
    persisted_provenance = data.get("metric_provenance")
    derived_provenance = _derive_latest_metric_provenance(history)
    if derived_provenance:
        state["metric_provenance"] = derived_provenance
    elif isinstance(persisted_provenance, dict) and persisted_provenance:
        state["metric_provenance"] = persisted_provenance
    if "metrics" in state:
        pass
    elif isinstance(derived_metrics, dict) and derived_metrics:
        state["metrics"] = derived_metrics
    elif isinstance(persisted_metrics, dict) and persisted_metrics:
        state["metrics"] = persisted_metrics
    if active_metrics_selected:
        provenance = dict(state.get("metric_provenance") or {})
        provenance.update({key: value for key, value in {
            "candidate_id": active.get("candidate_id"),
            "job_id": active.get("job_id"),
            "evidence_stage": active.get("measurement_stage"),
        }.items() if value is not None})
        best = lineage.get("best") if lineage else None
        if isinstance(best, dict):
            provenance["comparison"] = {
                "label": "measured best",
                "candidate_id": best.get("candidate_id"),
                "stage": best.get("measurement_stage"),
                "metrics": best.get("metrics"),
                "is_selected": best.get("candidate_id") == active.get("candidate_id"),
            }
        state["metric_provenance"] = provenance
    if isinstance(data.get("architecture_contract"), dict) and data.get("architecture_contract_hash"):
        state["architecture_contract"] = data["architecture_contract"]
        state["architecture_contract_hash"] = data["architecture_contract_hash"]
    if isinstance(data.get("design_contract"), dict):
        try:
            state["design_contract"] = _contract_state_from(data["design_contract"]).canonical()
            if isinstance(data.get("contract_revision_diff"), list):
                state["contract_revision_diff"] = data["contract_revision_diff"]
        except ContractProtocolError:
            logger.warning("[%s] ignoring invalid persisted design contract", session_id)
    if data.get("benchmark_contract_id") == "matmul3":
        state["benchmark_contract_id"] = "matmul3"
    timeline = _sanitize_timeline_list(data.get("timeline"))
    if timeline:
        state["timeline"] = timeline
    if (isinstance(data.get("run_state"), dict) and
            data["run_state"].get("version") == _RUN_STATE_VERSION):
        state["run_state"] = data["run_state"]
    if data.get("hardware_project"):
        try:
            state["hardware_project"] = hydrate_manifest(data["hardware_project"])
            state["hardware_project"]["module_index"] = build_module_index(state["hardware_project"])
            source_digest = state["hardware_project"]["digest"]
            provenance = state.get("metric_provenance")
            measured_digest = (
                provenance.get("hardware_project_digest")
                if isinstance(provenance, dict) else None
            )
            if measured_digest != source_digest:
                state.pop("metrics", None)
                state.pop("metric_provenance", None)
                state["measurement_status"] = {
                    "state": "stale", "reason": "source_set_changed",
                    "hardware_project_digest": source_digest,
                }
        except HardwareProjectError as exc:
            logger.error("[%s] invalid hardware project ignored: %s", session_id, exc)
            state["hardware_project_error"] = str(exc)
    return state


def _load_project(session_id: str) -> None:
    # Hold the same lock across file read and cache update.  A delete cannot
    # otherwise race this method and leave a project resurrected in memory.
    with _project_lock(session_id):
        try:
            data = _projects().load(session_id)
        except CorruptProjectError:
            logger.warning("[%s] corrupt project file, starting fresh", session_id)
            return
        if data is not None:
            _histories[session_id] = _sanitize_history(data.get("history", []))
            _project_timelines[session_id] = _sanitize_timeline_list(data.get("timeline"))
            _project_names[session_id] = data.get("name", session_id)
            if data.get("model"):
                _project_models[session_id] = data["model"]
            else:
                _project_models.pop(session_id, None)
            if data.get("reasoning_effort"):
                _project_efforts[session_id] = data["reasoning_effort"]
            else:
                _project_efforts.pop(session_id, None)
            if data.get("created"):
                _project_created[session_id] = data["created"]
            state = _get_state(session_id)
            if isinstance(data.get("usage_ledger"), dict):
                state["usage_ledger"] = data["usage_ledger"]
                totals = data["usage_ledger"].get("project", {})
                state["api_prompt_tokens"] = max(0, int(totals.get("input_tokens", 0) or 0))
                state["api_completion_tokens"] = max(0, int(totals.get("output_tokens", 0) or 0))
                state["api_total_tokens"] = max(0, int(totals.get("total_tokens", 0) or 0))
                state["tokens_used"] = state["api_total_tokens"]
                state["tokens_remaining"] = data["usage_ledger"].get("tokens_remaining")
            migrated = False
            persisted_run = data.get("run_state")
            if isinstance(persisted_run, dict) and persisted_run.get("version") == _RUN_STATE_VERSION:
                state["run_snapshot"] = dict(persisted_run)
                if persisted_run.get("lifecycle") == "running":
                    # A process restart cannot prove an in-flight generation/job survived.
                    state["run_snapshot"] = dict(persisted_run, lifecycle="interrupted",
                        terminal_reason="Backend restarted while the run was active.")
                    data["run_state"] = state["run_snapshot"]
                    migrated = True
            benchmark_id = (
                "matmul3" if data.get("benchmark_contract_id") == "matmul3"
                or _history_selects_frozen_matmul3(_histories[session_id])
                else None
            )
            migrated = migrated or (benchmark_id == "matmul3" and data.get("benchmark_contract_id") != benchmark_id)
            if benchmark_id:
                state["benchmark_contract_id"] = benchmark_id
                data["benchmark_contract_id"] = benchmark_id
            if isinstance(data.get("architecture_contract"), dict):
                state["architecture_contract"] = data["architecture_contract"]
                state["architecture_contract_hash"] = data.get("architecture_contract_hash")
            if isinstance(data.get("design_contract"), dict):
                try:
                    state["design_contract"] = _contract_state_from(
                        data["design_contract"]).canonical()
                    if isinstance(data.get("contract_revision_diff"), list):
                        state["contract_revision_diff"] = data["contract_revision_diff"]
                except ContractProtocolError:
                    logger.warning("[%s] ignoring invalid persisted design contract", session_id)
            if isinstance(data.get("pending_request"), str) and data["pending_request"].strip():
                state["pending_request"] = data["pending_request"]
            lineage = _validated_candidate_lineage(data.get("candidate_lineage"))
            if lineage:
                _restore_candidate_lineage(state, lineage)
            if not state.get("last_good_verilog"):
                # Projects with staged screens persist no active lineage
                # member; the durable SIMULATION PASSED transcript still
                # identifies the verified source, and CURRENT ARTIFACTS must
                # offer it to the model instead of a later unverified rewrite.
                durable = _durable_sim_checkpoint(_histories[session_id])
                if durable:
                    state["last_good_verilog"] = durable["code"]

            # Older matmul3 projects may have reached disk with the invalid
            # generic throughput (for example II=10 represented as 1.6). Once
            # the transcript proves the frozen benchmark, fail closed: discard
            # the invalid READY proposal and require a fresh canonical proposal.
            if benchmark_id == "matmul3":
                candidate = data.get("architecture_contract")
                if not isinstance(candidate, dict):
                    surface = data.get("design_contract")
                    spec = surface.get("spec") if isinstance(surface, dict) else None
                    goals = spec.get("timing_goals") if isinstance(spec, dict) else None
                    metadata = spec.get("metadata") if isinstance(spec, dict) else None
                    candidate = dict((metadata or {}).get("architecture_contract") or {})
                    if isinstance(spec, dict):
                        candidate.setdefault("top_module", spec.get("top_module"))
                        candidate.setdefault("technology", spec.get("target"))
                    candidate.setdefault("target_type", "asic")
                    candidate.update(goals or {})
                errors = _frozen_matmul3_admission_errors(
                    {"contract": candidate}, None, benchmark_contract_id=benchmark_id)
                if errors:
                    recovered = _latest_verified_frozen_matmul3_architecture(
                        _histories[session_id])
                    logger.warning(
                        "[%s] repairing invalid persisted frozen matmul3 contract: %s",
                        session_id, "; ".join(errors))
                    previous = state.get("design_contract")
                    old_revision = 0
                    if isinstance(previous, dict):
                        try:
                            old_revision = _contract_state_from(previous).revision
                        except ContractProtocolError:
                            pass
                    if recovered is not None:
                        surface = _extract_design_contract(
                            _design_spec_from_architecture(recovered),
                            project_id=session_id, session_id=session_id)
                        locked = _contract_state_from(surface["contract"])
                        locked.revision = old_revision + 1
                        locked.status = "locked"
                        locked.paused_reason = None
                        locked.pending_request = False
                        locked.digest = locked.recompute_digest()
                        restored = locked.canonical()
                        architecture = recovered.canonical()
                        architecture_hash = _architecture_contract_hash(recovered)
                        state.update({
                            "architecture_contract": architecture,
                            "architecture_contract_hash": architecture_hash,
                            "design_contract": restored,
                            "contract_revision_diff": _contract_revision_diff(
                                previous, restored) if isinstance(previous, dict) else None,
                            "pending_request": None,
                            "contract_gate_pending": False,
                            "contract_run_revision": locked.revision,
                            "contract_run_digest": locked.digest,
                        })
                        data.update({
                            "architecture_contract": architecture,
                            "architecture_contract_hash": architecture_hash,
                            "design_contract": restored,
                        })
                        data.pop("pending_request", None)
                        if state.get("contract_revision_diff"):
                            data["contract_revision_diff"] = state["contract_revision_diff"]
                        else:
                            data.pop("contract_revision_diff", None)
                    else:
                        # No simulation-backed contract can be trusted. Fail
                        # closed and require a fresh canonical proposal.
                        for key in ("architecture_contract", "architecture_contract_hash",
                                    "design_contract", "contract_revision_diff", "pending_request"):
                            data.pop(key, None)
                            state.pop(key, None)
                    migrated = True
            if migrated:
                _projects().save(session_id, data)
            # Repair/synthesis admission state is intentionally in memory, but
            # a successful simulation is durable evidence in the transcript.
            # Rebuild only those checkpoints after restart; never infer a pass
            # from lint, synthesis, or a model claim.
            _restore_simulation_checkpoints(session_id, _histories[session_id])


_project_names: dict[str, str] = {}
_project_models: dict[str, str] = {}
_project_efforts: dict[str, str] = {}
_project_created: dict[str, float] = {}


def list_projects() -> list[dict]:
    """List persisted projects, newest first."""
    out = []
    for project_id, data, path, mtime in _projects().iter_projects():
        history = data.get("history", [])
        preview = next(
            (m["content"] for m in reversed(history) if m.get("role") == "user"), ""
        )
        out.append({
            "id": project_id,
            "name": data.get("name", project_id),
            "model": data.get("model") or model_registry.DEFAULT_MODEL_KEY,
            "reasoning_effort": data.get("reasoning_effort") or None,
            # Last activity = file mtime; created falls back for old files.
            "created": data.get("created") or mtime,
            "updated": mtime,
            "messages": len([m for m in history if m.get("role") != "system"]),
            "preview": preview[:120],
        })
    out.sort(key=lambda p: p["updated"], reverse=True)
    return out


def create_project(name: str, model: str | None = None,
                   reasoning_effort: str | None = None) -> dict:
    """Create a project; `model` is an optional registry key and
    `reasoning_effort` an optional effort level for that model."""
    if model is not None:
        model_registry.get_model(model)  # validate; KeyError on unknown key
    effective_model = model or model_registry.DEFAULT_MODEL_KEY
    if reasoning_effort is not None:
        _validate_reasoning_effort(effective_model, reasoning_effort)
    session_id = f"proj-{uuid.uuid4().hex[:12]}"
    _histories[session_id] = [{"role": "system", "content": SYSTEM_INSTRUCTION}]
    _project_names[session_id] = name or "Untitled project"
    _project_created[session_id] = time.time()
    if model is not None:
        _project_models[session_id] = model
    if reasoning_effort is not None:
        _project_efforts[session_id] = reasoning_effort
    _save_project(session_id)
    return {"id": session_id, "name": _project_names[session_id],
            "model": _project_models.get(session_id, model_registry.DEFAULT_MODEL_KEY),
            "reasoning_effort": _project_efforts.get(session_id),
            "created": _project_created[session_id],
            "updated": time.time(), "messages": 0, "preview": ""}


def delete_project(session_id: str) -> bool:
    path = _project_path(session_id)
    with _project_lock(session_id):
        _histories.pop(session_id, None)
        _project_names.pop(session_id, None)
        _project_models.pop(session_id, None)
        _project_efforts.pop(session_id, None)
        _project_created.pop(session_id, None)
        _project_timelines.pop(session_id, None)
        _session_state.pop(session_id, None)
        return _projects().delete(session_id) if path else False


def rename_project(session_id: str, name: str) -> dict | None:
    """Rename a persisted project; return its updated summary."""
    return update_project(session_id, name=name)


def update_project(session_id: str, name: str | None = None,
                   model: str | None = None,
                   reasoning_effort=UNSET) -> dict | None:
    """Update name, model key, and/or reasoning effort of a project.

    ``reasoning_effort`` uses the UNSET sentinel: absent means unchanged,
    None means clear back to the registry default, a string must be accepted
    by the project's (possibly just-changed) model.
    """
    path = _project_path(session_id)
    if not path:
        return None
    if model is not None:
        model_registry.get_model(model)  # validate; KeyError on unknown key
    effective_model = model or _project_models.get(
        session_id, model_registry.DEFAULT_MODEL_KEY)
    if reasoning_effort is not UNSET and reasoning_effort is not None:
        _validate_reasoning_effort(effective_model, reasoning_effort)
    with _project_lock(session_id):
        # Check existence under the same lock as delete.  Otherwise a delete
        # between the check and this section could resurrect a project.
        if not _projects().exists(session_id):
            return None
        _get_history(session_id)  # keep the in-memory history in the snapshot
        if name is not None:
            _project_names[session_id] = name
        if model is not None:
            _project_models[session_id] = model
            # A stored effort that the new model does not accept falls back to
            # that model's default instead of poisoning later completions.
            if (_project_efforts.get(session_id) is not None
                    and _project_efforts[session_id]
                    not in (model_registry.get_model(model).get("reasoning_efforts") or [])):
                _project_efforts.pop(session_id, None)
        if reasoning_effort is not UNSET:
            if reasoning_effort is None:
                _project_efforts.pop(session_id, None)
            else:
                _project_efforts[session_id] = reasoning_effort
        _save_project(session_id)
        return {"id": session_id, "name": _project_names[session_id],
                "model": _project_models.get(session_id, model_registry.DEFAULT_MODEL_KEY),
                "reasoning_effort": _project_efforts.get(session_id),
                "updated": os.path.getmtime(path)}


def get_hardware_project(session_id: str) -> dict | None:
    """Return the hydrated active source set, or None for a legacy project."""
    try:
        data = _projects().load(session_id)
    except CorruptProjectError:
        return None
    if not data or not data.get("hardware_project"):
        return None
    project = hydrate_manifest(data["hardware_project"])
    project["module_index"] = build_module_index(project)
    return project


def replace_hardware_project(session_id: str,
                             update: HardwareProjectUpdate) -> dict | None:
    """Atomically replace one project's validated source-set manifest."""
    path = _project_path(session_id)
    if not path:
        return None
    with _project_lock(session_id):
        data = _projects().load(session_id)
        if data is None:
            return None
        previous_manifest = data.get("hardware_project")
        revision = 1
        if isinstance(previous_manifest, dict):
            try:
                revision = int(validate_manifest(previous_manifest)["revision"]) + 1
            except (HardwareProjectError, TypeError, ValueError, KeyError):
                raise HardwareProjectError(
                    "active hardware project manifest is invalid")
        manifest = build_manifest(update, revision=revision)
        validate_manifest(manifest)
        next_data = dict(data)
        next_data["hardware_project"] = manifest
        for stale_key in ("latest_metrics", "metric_provenance",
                          "candidate_lineage"):
            next_data.pop(stale_key, None)
        top_source = next((
            item for item in update.files
            if item.role in {"rtl", "package"} and
            re.search(rf"\bmodule\s+{re.escape(update.top_module)}\b",
                      item.content)
        ), None)
        if top_source is not None:
            next_data["latest_verilog"] = {
                "top": update.top_module, "code": top_source.content,
            }
        _projects().save(session_id, next_data)
        return hydrate_manifest(manifest)


# Metric line patterns mirrored from the frontend's extractMetrics (the raw
# tool-result text is what gets persisted in history, so we re-parse it here).
_METRIC_PATTERNS = {
    "cellarea": re.compile(r"cellarea:\s*([0-9.eE+-]+)"),
    "totalarea": re.compile(r"totalarea:\s*([0-9.eE+-]+)"),
    "utilization": re.compile(r"utilization:\s*([0-9.eE+-]+)"),
    "cells": re.compile(r"(?:^|\n)cells:\s*(\d+)"),
    "power": re.compile(r"peakpower:\s*([0-9.eE+-]+)"),
    "setupslack": re.compile(r"setupslack:\s*([0-9.eE+-]+)"),
    "holdslack": re.compile(r"holdslack:\s*([0-9.eE+-]+)"),
    "fmax": re.compile(r"(?:fmax|fmax_mhz):\s*([0-9.eE+-]+)"),
}

_METRIC_PROVENANCE_KEYS = ("job_id", "run_id", "candidate_id", "evidence_stage", "stage", "fmax_provenance", "fmax_source")


def _metric_snapshot_from_text(text: str) -> tuple[dict, dict]:
    """Parse one result as a complete metric snapshot, never a delta."""
    text = str(text or "")
    values, provenance = {}, {}
    for line in text.splitlines():
        if line.lower().startswith("job_id:") and line.split(":", 1)[1].strip():
            provenance["job_id"] = line.split(":", 1)[1].strip()
        try:
            payload = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        source = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else payload
        for key in ("cellarea", "totalarea", "utilization", "cells", "power", "peakpower", "setupslack", "holdslack", "fmax", "fmax_mhz", "wns_ns"):
            if key not in source or source[key] is None:
                continue
            target = "fmax" if key == "fmax_mhz" else ("setupslack" if key == "wns_ns" else key)
            try:
                value = float(source[key])
            except (TypeError, ValueError):
                continue
            values[target] = int(value) if target == "cells" else value
        for key in _METRIC_PROVENANCE_KEYS:
            if payload.get(key) is not None:
                provenance[key] = payload[key]
        search = payload.get("fmax_search") or payload.get("search")
        if isinstance(search, dict):
            provenance.setdefault("fmax_provenance", search)
            provenance.setdefault("evidence_stage", "fmax_search")
    for key, pattern in _METRIC_PATTERNS.items():
        match = pattern.search(text)
        if match:
            try:
                value = float(match.group(1))
            except ValueError:
                values[key] = match.group(1)
                continue
            values[key] = int(value) if key == "cells" else value
    if isinstance(values.get("fmax"), (int, float)) and values["fmax"] > 1000:
        # The tool prints fmax in Hz; the dashboard convention is MHz. The
        # regex path used to keep the raw string, skipping this conversion —
        # a reloaded project then showed "52,510,000 MHz".
        values["fmax"] /= 1e6
    # Critical-path delay: the synthesis+STA screen never prints it as its own
    # metric, but for a single-clock constrained design it is exactly the
    # clock period minus the worst setup slack. Flag the derivation so the UI
    # labels it instead of passing it off as a measured value.
    period = re.search(r'"clock_period_ns"\s*:\s*([0-9.eE+-]+)', text)
    if ("criticalpath" not in values and period is not None
            and isinstance(values.get("setupslack"), (int, float))):
        try:
            values["criticalpath"] = round(
                float(period.group(1)) - float(values["setupslack"]), 3)
            values["criticalpath_derived"] = True
        except (TypeError, ValueError):
            pass
    if values and "evidence_stage" not in provenance:
        provenance["evidence_stage"] = "fmax_search" if "fmax_provenance" in provenance else "screening_sta"
    return values, provenance


def _valid_dashboard_verilog(value: object) -> dict | None:
    """Return a safe full RTL dashboard snapshot, rejecting compact handles."""
    if not isinstance(value, dict):
        return None
    code = value.get("code")
    if not isinstance(code, str) or not code.strip() or _looks_like_artifact_handle(code):
        return None
    top = value.get("top") or "top"
    return {"top": str(top), "code": code}


def _derive_latest_metric_provenance(history: list[dict]) -> dict:
    """Return provenance belonging to the latest complete metric result."""
    result = {}
    for entry in history:
        if entry.get("role") != "tool":
            continue
        values, provenance = _metric_snapshot_from_text(str(entry.get("content") or ""))
        if values:
            result = provenance
    return result


def _derive_latest_state(history: list[dict], *, fallback: dict | None = None) -> tuple[dict | None, dict]:
    """Persist the latest passing RTL in preference to failed candidates.

    Also derives the last structured lint report (``write_and_check_syntax``)
    and binds it to the final RTL: ``bound`` is true only when the linted
    code digest matches the dashboard RTL digest, so a stale lint (older RTL
    revision) is distinguishable from current evidence.
    """
    latest = None
    passing = None
    metrics: dict[str, str] = {}
    lint: dict | None = None
    calls: dict[str, tuple[str, dict]] = {}
    for entry in history:
        if entry.get("role") == "assistant":
            for call in entry.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    args = {}
                if not isinstance(args, dict):
                    continue
                call_id = call.get("id")
                name = fn.get("name") if isinstance(fn, dict) else None
                if call_id and name:
                    calls[str(call_id)] = (str(name), args)
                if isinstance(args.get("verilog_code"), str) and args["verilog_code"].strip():
                    resolved, error = _resolve_artifact_handles_from_history(history, args)
                    if error is None and isinstance(resolved.get("verilog_code"), str):
                        latest = {"top": resolved.get("top_module") or "top", "code": resolved["verilog_code"]}
        elif entry.get("role") == "tool":
            content = str(entry.get("content") or "")
            item = calls.get(str(entry.get("tool_call_id")))
            if item and item[0] == "run_simulation" and "SIMULATION PASSED" in content:
                resolved, error = _resolve_artifact_handles_from_history(history, item[1])
                if error is None and isinstance(resolved.get("verilog_code"), str):
                    passing = {"top": resolved.get("top_module") or "top", "code": resolved["verilog_code"]}
            if item and item[0] == "write_and_check_syntax":
                snapshot = _lint_snapshot_from_text(content)
                if snapshot:
                    lint = snapshot
            parsed_metrics, _ = _metric_snapshot_from_text(content)
            if parsed_metrics:
                # One result is one compatible snapshot; never mix jobs.
                metrics = parsed_metrics
    verilog = passing or latest or _valid_dashboard_verilog(fallback)
    if lint and verilog:
        lint = {**lint,
                "bound": bool(lint.get("code_sha256"))
                and lint.get("code_sha256") == hashlib.sha256(
                    str(verilog.get("code") or "").encode()).hexdigest()}
    return verilog, metrics, lint


def _lint_snapshot_from_text(content: str) -> dict | None:
    """Extract the compact lint verdict from a write_and_check_syntax result.

    The result is a JSON report, but budget notices or a cache marker may be
    appended around it, so the fields are picked with anchored patterns
    instead of a strict parse. Returns None for anything unparseable.
    """
    text = str(content or "")
    verdict = re.search(r'"verdict"\s*:\s*"([a-z_]+)"', text)
    ok = re.search(r'"ok"\s*:\s*(true|false)', text)
    digest = re.search(r'"code_sha256"\s*:\s*"([0-9a-f]{64})"', text)
    if not verdict and not ok:
        return None
    return {
        "verdict": verdict.group(1) if verdict else None,
        "ok": ok.group(1) == "true" if ok else None,
        "code_sha256": digest.group(1) if digest else None,
    }


def _session_model_key(session_id: str) -> str:
    """Registry key for a session: the project's stored model, else default."""
    with _project_lock(session_id):
        _get_history(session_id)  # loads the project (and its model) from disk
        return _project_models.get(session_id, model_registry.DEFAULT_MODEL_KEY)


# Reasoning tokens consume the same run budget as answer tokens, but a higher
# effort level spends far more of them per round (measured ~30k thinking tokens
# on one small-design round at GLM's platform-default "high"). Scale the run
# budget with the session's effective effort so raising the effort buys real
# room instead of just moving where runs die.
EFFORT_BUDGET_MULTIPLIERS = {"low": 1.0, "high": 2.0, "max": 4.0}


def _validate_reasoning_effort(model_key: str, effort: str) -> str:
    """Validate an effort level against a model's declared list.

    Raises KeyError (mapped to 422 like an unknown model key) when the model
    does not accept the value.
    """
    entry = model_registry.get_model(model_key)  # KeyError on unknown model
    if effort not in (entry.get("reasoning_efforts") or []):
        raise KeyError(
            f"reasoning_effort '{effort}' is not supported by model '{model_key}'")
    return effort


def _session_reasoning_effort(session_id: str) -> str | None:
    """Effective reasoning effort for a session's model.

    Precedence: the project's stored setting, then the registry default
    (env-overridable per machine). Only models that declare effort levels
    can yield a value; None otherwise.
    """
    try:
        entry = model_registry.get_model(_session_model_key(session_id))
    except Exception:
        return None
    efforts = entry.get("reasoning_efforts") or []
    stored = _project_efforts.get(session_id)
    if stored in efforts:
        return stored
    if not entry.get("reasoning"):
        return None
    # The registry default may carry a machine env override; drop values the
    # provider would reject instead of sending them.
    default = entry.get("reasoning_effort_default")
    return default if default in efforts else None


def _chat_budget(session_id: str) -> int:
    """Run token budget for a session, scaled by its reasoning effort."""
    effort = _session_reasoning_effort(session_id)
    return max(1, int(CHAT_TOKEN_BUDGET
                      * EFFORT_BUDGET_MULTIPLIERS.get(effort, 1.0)))


def _session_model_reasoning(session_id: str) -> bool:
    """Whether the session's model spends completion tokens on reasoning.

    Drives the per-request output ceiling: a reasoning model's private
    thinking shares the completion budget with the visible answer.
    """
    try:
        entry = model_registry.get_model(_session_model_key(session_id))
        return bool(entry.get("reasoning"))
    except Exception:
        return False


def _continuation_request_kwargs(session_id: str) -> dict:
    """Provider-specific knobs for the bounded empty-length continuation.

    A reasoning model that exhausted its output budget on thinking will do
    so again on an identical retry (its reasoning is never replayed in the
    prompt). When the registry entry declares effort levels, the one-shot
    continuation asks the provider for low-effort reasoning so the retry
    can actually fit the answer or tool call inside the ceiling. Sent via
    ``extra_body`` so plain OpenAI SDK signatures accept it.
    """
    try:
        entry = model_registry.get_model(_session_model_key(session_id))
    except Exception:
        return {}
    if entry.get("reasoning") and "low" in (entry.get("reasoning_efforts") or []):
        return {"extra_body": {"reasoning_effort": "low"}}
    return {}


def _model_effort_kwargs(session_id: str) -> dict:
    """Per-model default reasoning effort for every request of a session.

    GLM 5.3 always thinks, and the platform's default effort is high —
    measured ~30k thinking tokens on a single small-design round, which
    exhausts the run token budget before the flow finishes. The effective
    effort comes from the project setting or the registry default (see
    _session_reasoning_effort); it is already validated against the entry's
    declared list.
    """
    effort = _session_reasoning_effort(session_id)
    if effort:
        return {"extra_body": {"reasoning_effort": effort}}
    return {}


def _client_for(session_id: str) -> tuple[AsyncOpenAI, str]:
    """Build the client + provider model id for a session's registry entry.

    Raises RuntimeError with a clear message when the entry's API-key env var
    is not set — chat() turns that into an error frame instead of crashing.
    """
    key = _session_model_key(session_id)
    entry = model_registry.get_model(key)
    api_key = os.environ.get(entry["api_key_env"])
    if not api_key:
        raise RuntimeError(
            f"model '{key}' unavailable: env var {entry['api_key_env']} not set")
    kwargs = {"api_key": api_key, "base_url": entry["base_url"],
              "timeout": httpx.Timeout(LLM_TIMEOUT_S, connect=10.0),
              "max_retries": 2}
    if entry.get("extra_headers"):
        kwargs["default_headers"] = entry["extra_headers"]
    return AsyncOpenAI(**kwargs), entry["model_id"]


def _get_history(session_id: str) -> list[dict]:
    with _project_lock(session_id):
        if session_id not in _histories:
            _load_project(session_id)
        if session_id not in _histories:
            _histories[session_id] = [{"role": "system", "content": SYSTEM_INSTRUCTION}]
        return _histories[session_id]


def extract_design_contract(session_id: str, spec, *, project_id: str | None = None) -> dict:
    """Create and persist a DesignSpec-backed draft for a project session."""
    project_id = project_id or session_id
    contract = _extract_design_contract(spec, project_id=project_id, session_id=session_id)
    parsed = _contract_state_from(contract["contract"])
    _get_state(session_id)["design_contract"] = parsed.canonical()
    _save_project(session_id)
    return contract


def _design_spec_from_architecture(contract: ArchitectureContract) -> DesignSpec:
    """Project the approved microarchitecture into the canonical UI spec."""
    return DesignSpec(
        design_id=contract.top_module,
        name=contract.top_module,
        top_module=contract.top_module,
        target=contract.technology,
        timing_goals={
            "clock_target_mhz": contract.clock_target_mhz,
            "clock_period_ns": contract.clock_period_ns,
            "latency_cycles": contract.latency_cycles,
            "throughput_per_cycle": contract.throughput_per_cycle,
            "pipeline_stages": contract.pipeline_stages,
        },
        assumptions=[contract.rationale] if contract.rationale else [],
        metadata={
            "architecture_contract": contract.canonical(),
            "inferred_defaults": [],
        },
    )


def _contract_revision_diff(previous: dict, current: dict) -> list[dict]:
    """Return compact engineering-field changes between contract revisions."""
    fields = (
        ("Timing", "clock_target_mhz", "Clock target"),
        ("Timing", "clock_period_ns", "Clock period"),
        ("Data semantics", "latency_cycles", "Latency"),
        ("Data semantics", "throughput_per_cycle", "Throughput"),
        ("Data semantics", "pipeline_stages", "Pipeline stages"),
        ("Data semantics", "multiplier_strategy", "Multiplier"),
        ("Data semantics", "memory_strategy", "Memory"),
        ("Target", "technology", "Technology"),
    )
    def value(contract, field):
        spec = contract.get("spec") or {}
        goals = spec.get("timing_goals") or {}
        metadata = spec.get("metadata") or {}
        arch = metadata.get("architecture_contract") or {}
        return goals.get(field, arch.get(field, spec.get(field)))
    result = []
    for section, field, label in fields:
        before, after = value(previous, field), value(current, field)
        if before is not None and after is not None and before != after:
            result.append({"section": section, "field": field, "label": label,
                           "previous": before, "next": after})
    return result


def get_design_contract(session_id: str) -> dict:
    """Return the authoritative persisted GenUI contract state, if present."""
    state = _get_state(session_id)
    raw = state.get("design_contract")
    if not isinstance(raw, dict):
        return {}
    try:
        from genui import state_payload
        payload = state_payload(_contract_state_from(raw))
        diff = state.get("contract_revision_diff")
        if not (isinstance(diff, list) and diff):
            # Backfill projects created before durable diff persistence by
            # recovering the immediately preceding contract snapshot from the
            # transcript's authoritative tool result.
            current_revision = int(raw.get("revision") or 0)
            previous = None
            for entry in _get_history(session_id):
                if entry.get("role") != "tool":
                    continue
                text = str(entry.get("content") or "").lstrip()
                if not text.startswith("{"):
                    continue
                try:
                    candidate, _ = json.JSONDecoder().raw_decode(text)
                except json.JSONDecodeError:
                    continue
                contract = candidate.get("contract") if isinstance(candidate, dict) else None
                if isinstance(contract, dict) and int(contract.get("revision") or 0) == current_revision - 1:
                    previous = contract
            if isinstance(previous, dict):
                diff = _contract_revision_diff(previous, raw)
                if diff:
                    state["contract_revision_diff"] = diff
                    _save_project(session_id)
        if isinstance(diff, list) and diff:
            payload["revision_diff"] = diff
        return payload
    except ContractProtocolError:
        return {}


def get_pending_contract_request(session_id: str) -> str | None:
    """Return a pending original request only for the exact locked contract."""
    state = _get_state(session_id)
    pending = state.get("pending_request")
    contract = state.get("design_contract")
    if not isinstance(pending, str) or not pending.strip() or not isinstance(contract, dict):
        return None
    try:
        parsed = _contract_state_from(contract)
    except ContractProtocolError:
        return None
    if parsed.status not in {"locked", "approved"}:
        return None
    if parsed.pending_request is False:
        return None
    return pending


def _contract_material_fingerprint(contract: dict) -> str:
    """Hash engineering fields while excluding review-only/cosmetic metadata."""
    value = json.loads(json.dumps(contract, sort_keys=True, default=str))
    spec = value.get("spec", {}) if isinstance(value, dict) else {}
    if isinstance(spec, dict):
        metadata = spec.get("metadata")
        if isinstance(metadata, dict):
            metadata = dict(metadata)
            metadata.pop("inferred_defaults", None)
            architecture = metadata.get("architecture_contract")
            if isinstance(architecture, dict):
                architecture = dict(architecture)
                architecture.pop("rationale", None)
                metadata["architecture_contract"] = architecture
            spec["metadata"] = metadata
        spec.pop("assumptions", None)
        value["spec"] = spec
    # Review/lifecycle metadata is not engineering intent. A rationale or
    # display-only revision must not erase simulation/timing evidence.
    for key in ("paused_reason", "pending_request", "digest", "status",
                "revision", "project_id", "session_id", "schema_version"):
        value.pop(key, None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _invalidate_contract_checkpoints(state: dict) -> None:
    """Invalidate every RTL/evidence checkpoint after a material revision."""
    state["revisions"] = []
    state["revision_by_hash"] = {}
    state["best_revision"] = None
    state["active_revision"] = None
    state["latest_attempted_revision"] = None
    state["last_good_verilog"] = None
    state["screen_best_revision"] = None
    state["promoted_revision"] = None
    state["promotion_baseline"] = None
    state["non_improving_attempts"] = 0
    state["timing_violations"] = 0
    state["timing_diagnosis"] = None
    state["contract_run_revision"] = None
    state["contract_run_digest"] = None


def _sync_architecture_binding(contract: object) -> tuple[dict, str] | None:
    """Rebuild the executable ArchitectureContract from the locked DesignSpec."""
    try:
        spec = contract.spec
        metadata = dict(spec.metadata or {})
        raw = metadata.get("architecture_contract")
        if not isinstance(raw, dict):
            return None
        payload = dict(raw)
        goals = dict(spec.timing_goals or {})
        payload.update({
            "top_module": spec.top_module,
            "technology": spec.target,
            **{key: goals[key] for key in (
                "clock_target_mhz", "clock_period_ns", "latency_cycles",
                "throughput_per_cycle", "pipeline_stages") if key in goals},
        })
        refreshed = ArchitectureContract.model_validate(payload)
        canonical = refreshed.canonical()
        return canonical, _architecture_contract_hash(refreshed)
    except Exception as exc:
        raise ContractProtocolError(
            f"revision cannot produce a valid architecture binding: {exc}",
            code="invalid_architecture") from exc


def apply_design_contract_action(session_id: str, *, project_id: str, action: str,
                                 expected_revision, expected_digest, payload=None) -> dict:
    """Apply a typed contract action and persist the resulting authoritative state."""
    state = _get_state(session_id).get("design_contract")
    if not isinstance(state, dict):
        raise ContractProtocolError("no design contract exists for this session", code="missing_contract")
    before = _contract_state_from(state)
    result = _apply_contract_action(
        state, action=action, project_id=project_id, session_id=session_id,
        expected_revision=expected_revision, expected_digest=expected_digest, payload=payload)
    parsed = _contract_state_from(result["contract"])
    state = _get_state(session_id)
    state["design_contract"] = parsed.canonical()
    if action == "contract.revise":
        binding = _sync_architecture_binding(parsed)
        if binding is not None:
            state["architecture_contract"], state["architecture_contract_hash"] = binding
    if action in {"contract.approve", "contract.approve_and_build"} and parsed.status == "locked":
        state["contract_run_revision"] = parsed.revision
        state["contract_run_digest"] = parsed.digest
        # The request remains pending until the resumed chat has been admitted.
    elif action == "contract.revise":
        if _contract_material_fingerprint(before.canonical()) != _contract_material_fingerprint(parsed.canonical()):
            _invalidate_contract_checkpoints(state)
        state["contract_run_revision"] = None
        state["contract_run_digest"] = None
    _save_project(session_id)
    return result


def reset_session(session_id: str) -> None:
    with _project_lock(session_id):
        _histories.pop(session_id, None)
        _project_timelines.pop(session_id, None)
        _session_state.pop(session_id, None)


def _truncate(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + f"…[{len(text) - limit} chars truncated]"


def _truncate_middle(text: str, limit: int) -> str:
    """Bound large source/log values while retaining both useful ends."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    if limit <= 80:
        return text[:limit]
    head = (limit * 2) // 3
    tail = limit - head
    omitted = len(text) - head - tail
    return text[:head] + f"\n…[{omitted} chars omitted by model context bound]…\n" + text[-tail:]


_CONTEXT_ARTIFACT_KEYS = (
    ("verilog_code", "rtl"),
    ("testbench_code", "testbench"),
    ("wrapper_verilog", "wrapper"),
    ("reference_rtl_code", "reference RTL"),
)

# Source blobs are compacted in the provider-facing protocol with this exact
# handle. Handles are references, never paths or model-controlled locations:
# the only source of truth is a matching, durable assistant tool-call argument
# from this session's history. Keep this grammar deliberately strict so a
# model cannot smuggle an arbitrary path or a prose placeholder into an EDA
# tool call.
_ARTIFACT_HANDLE_RE = re.compile(
    r"^\[artifact (?P<label>[A-Za-z][A-Za-z0-9 _-]*):"
    r"(?P<prefix>[0-9a-fA-F]{16,64}) omitted from protocol tail; "
    r"see CURRENT ARTIFACTS\]$")
_ARTIFACT_LABEL_ALIASES = {
    "rtl": "rtl",
    "verilog": "rtl",
    "testbench": "testbench",
    "tb": "testbench",
    "wrapper": "wrapper",
    "reference rtl": "reference RTL",
    "reference_rtl": "reference RTL",
    "reference-rtl": "reference RTL",
}


def _artifact_handle_marker(label: str, code: str) -> str:
    """Return the canonical compact representation used in model context."""
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    return (f"[artifact {label}:{digest[:16]} omitted from protocol tail; "
            "see CURRENT ARTIFACTS]")


def _parse_artifact_handle(value: object) -> tuple[str, str] | None:
    """Parse a server-generated handle, returning (label, hash prefix).

    ``None`` means this is ordinary source, not a handle. Callers separately
    detect strings beginning with ``[artifact`` so malformed handles fail
    closed rather than being passed to a compiler as source text.
    """
    if not isinstance(value, str):
        return None
    match = _ARTIFACT_HANDLE_RE.fullmatch(value)
    if not match:
        return None
    raw_label = match.group("label").strip().lower()
    label = _ARTIFACT_LABEL_ALIASES.get(raw_label)
    if label is None:
        return None
    return label, match.group("prefix").lower()


def _looks_like_artifact_handle(value: object) -> bool:
    return isinstance(value, str) and value.startswith("[artifact ")


def _entry_tool_calls(entry: dict) -> list[dict]:
    calls = entry.get("tool_calls") or []
    return calls if isinstance(calls, list) else []


def _call_args(call: dict) -> dict:
    fn = call.get("function") or {}
    raw = fn.get("arguments", "{}") if isinstance(fn, dict) else "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# OpenAI-compatible providers sometimes attach private chain-of-thought fields
# (for example ``reasoning``/``reasoning_content``) to assistant messages.
# Those fields are neither needed to continue a tool protocol exchange nor
# safe to replay to another completion. Keep the durable transcript and the
# provider-facing context to the small, interoperable message vocabulary that
# the chat-completions API actually consumes.
_MESSAGE_KEYS = {
    "system": {"role", "content", "name"},
    "developer": {"role", "content", "name"},
    "user": {"role", "content", "name"},
    "assistant": {"role", "content", "tool_calls", "name", "refusal"},
    "tool": {"role", "content", "tool_call_id", "name"},
}


def _artifact_catalog(history: list[dict]) -> dict[str, dict[str, list[str]]]:
    """Index genuine source snapshots in durable assistant tool calls.

    The value is ``label -> full sha256 -> [source variants]``. A prefix is
    accepted only when it identifies one full digest and one source body.
    Keeping variants lets us fail closed even in the astronomically unlikely
    event of a hash collision or corrupted history.
    """
    catalog: dict[str, dict[str, list[str]]] = {}
    for entry in history or []:
        if entry.get("role") != "assistant":
            continue
        for call in _entry_tool_calls(entry):
            args = _call_args(call)
            for key, label in _CONTEXT_ARTIFACT_KEYS:
                code = args.get(key)
                if not isinstance(code, str) or not code.strip():
                    continue
                # A previous compact marker is not a durable source snapshot.
                if _looks_like_artifact_handle(code):
                    continue
                digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
                by_digest = catalog.setdefault(label, {})
                variants = by_digest.setdefault(digest, [])
                if code not in variants:
                    variants.append(code)
    return catalog


def _resolve_artifact_handles_from_history(
        history: list[dict], args: dict) -> tuple[dict, str | None]:
    """Resolve compact source handles against durable history, fail-closed.

    This function deliberately returns a copied argument mapping. The caller
    can retain the compact mapping for UI/logging while dispatching only the
    resolved mapping to admission, checkpointing, and EDA tools.
    """
    resolved = dict(args or {})
    catalog = _artifact_catalog(history)
    for key, expected_label in _CONTEXT_ARTIFACT_KEYS:
        value = resolved.get(key)
        if not isinstance(value, str):
            continue
        if not _looks_like_artifact_handle(value):
            continue
        parsed = _parse_artifact_handle(value)
        if parsed is None:
            return resolved, (f"invalid artifact handle in {key}; source handles "
                             "must be generated by the server")
        label, prefix = parsed
        if label != expected_label:
            return resolved, (f"artifact handle field mismatch: {key} references "
                             f"{label}, expected {expected_label}")
        matches = [(digest, bodies) for digest, bodies in
                   catalog.get(expected_label, {}).items()
                   if digest.startswith(prefix)]
        if not matches:
            available = sorted({digest[:16] for digest in
                                catalog.get(expected_label, {})})
            available_text = (", ".join(available[-4:])
                              if available else "none")
            return resolved, (
                f"the requested {expected_label} source snapshot "
                f"{prefix} is not available in this session's saved tool-call "
                f"history. Available {expected_label} snapshot hashes: "
                f"{available_text}. Use the exact current handle from "
                "CURRENT ARTIFACTS, or send the complete source inline; "
                "do not invent or edit an artifact handle.")
        if len(matches) != 1:
            return resolved, (f"artifact handle for {key} is ambiguous: hash "
                             f"prefix {prefix} matches {len(matches)} durable "
                             f"snapshots")
        digest, bodies = matches[0]
        if len(bodies) != 1:
            return resolved, (f"artifact handle for {key} is ambiguous: the "
                             f"durable snapshot {digest[:16]} has conflicting bodies")
        code = bodies[0]
        # Defense in depth: the catalog is content-addressed, and this check
        # prevents a future catalog refactor from weakening the trust boundary.
        if hashlib.sha256(code.encode("utf-8")).hexdigest() != digest:
            return resolved, f"artifact handle for {key} failed content-hash validation"
        resolved[key] = code
    return resolved, None


def _resolve_artifact_handles(session_id: str, args: dict) -> tuple[dict, str | None]:
    """Resolve source handles using the session's durable transcript."""
    with _project_lock(session_id):
        history = list(_get_history(session_id))
    return _resolve_artifact_handles_from_history(history, args)


def _compact_tool_call_args(args: dict) -> dict:
    """Keep source out of public tool-call frames while preserving real args."""
    compact = dict(args or {})
    for key, label in _CONTEXT_ARTIFACT_KEYS:
        value = compact.get(key)
        if isinstance(value, str) and len(value) > 1200 and not _looks_like_artifact_handle(value):
            compact[key] = _artifact_handle_marker(label, value)
    return compact


def _sanitize_message(entry: object) -> dict:
    """Strip provider-private message fields while preserving tool protocol."""
    if not isinstance(entry, dict):
        return {}
    role = str(entry.get("role") or "").strip().lower()
    allowed = _MESSAGE_KEYS.get(role)
    if not allowed:
        return {}
    result = {key: entry[key] for key in allowed if key in entry}
    result["role"] = role

    # Normalize tool calls to the wire shape. In particular, do not carry
    # arbitrary fields nested inside a provider's function-call object.
    if role == "assistant" and "tool_calls" in result:
        calls = result.get("tool_calls")
        clean_calls = []
        for call in calls if isinstance(calls, list) else []:
            if not isinstance(call, dict):
                continue
            clean = {key: call[key] for key in ("id", "type") if key in call}
            function = call.get("function")
            if isinstance(function, dict):
                clean["function"] = {
                    key: function[key] for key in ("name", "arguments")
                    if key in function
                }
            if clean.get("function", {}).get("name"):
                clean_calls.append(clean)
        if clean_calls:
            result["tool_calls"] = clean_calls
        else:
            result.pop("tool_calls", None)
    return result


def _sanitize_history(history: object) -> list[dict]:
    """Return a clean durable transcript without mutating the caller list."""
    if not isinstance(history, list):
        return []
    return [clean for entry in history
            if (clean := _sanitize_message(entry)) and clean.get("role")]


def _latest_context_artifacts(history: list[dict], state: dict | None = None) -> dict[str, dict]:
    """Extract source snapshots, preferring the latest simulation-passing RTL."""
    artifacts: dict[str, dict] = {}
    for entry in history:
        if entry.get("role") != "assistant":
            continue
        for call in _entry_tool_calls(entry):
            args = _call_args(call)
            top = args.get("top_module") or "top"
            for key, label in _CONTEXT_ARTIFACT_KEYS:
                code = args.get(key)
                if not isinstance(code, str) or not code.strip():
                    continue
                if _looks_like_artifact_handle(code):
                    continue
                digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
                artifacts[label] = {
                    "kind": label,
                    "top": top,
                    "sha256": digest,
                    "code": code,
                    "chars": len(code),
                }
    # Failed optimization candidates may appear later in the transcript than
    # the last passing source. Keep the verified checkpoint as the primary
    # repair artifact so the next model turn starts from executable RTL.
    good = (state or {}).get("last_good_verilog")
    if isinstance(good, str) and good.strip():
        top = (state or {}).get("current_revision", {}).get("top_module", "top") if isinstance((state or {}).get("current_revision"), dict) else "top"
        artifacts["rtl"] = {"kind": "rtl", "top": top, "sha256": hashlib.sha256(good.encode()).hexdigest(), "code": good, "chars": len(good)}
    return artifacts


def _tool_call_name_map(history: list[dict]) -> dict[str, str]:
    result = {}
    for entry in history:
        if entry.get("role") != "assistant":
            continue
        for call in _entry_tool_calls(entry):
            fn = call.get("function") or {}
            call_id = call.get("id")
            name = fn.get("name") if isinstance(fn, dict) else None
            if call_id and name:
                result[call_id] = name
    return result


def _structured_event(event_type: str, phase: str, action: str,
                      status: str = "running", *, reason: str = "",
                      evidence: str = "", **extra) -> dict:
    """Return a concise public run event; never expose model reasoning or source."""
    payload = {
        "phase": _truncate(str(phase), 80),
        "action": _truncate(str(action), 180),
        "status": _truncate(str(status), 40),
        "event_id": f"{event_type}-{time.monotonic_ns()}",
        "timestamp": time.time(),
    }
    if reason:
        payload["reason"] = _truncate(str(reason), 260)
    if evidence:
        payload["evidence"] = _truncate(str(evidence), 320)
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    # Keep convenient aliases for API consumers that do not want to know the
    # nested budget envelope, while retaining the canonical ``budgets`` shape.
    if isinstance(payload.get("budgets"), dict):
        payload.setdefault("token_budget", payload["budgets"].get("tokens"))
        payload.setdefault("tool_budget", payload["budgets"].get("tools"))
        payload.setdefault("round_budget", payload["budgets"].get("rounds"))
    if isinstance(payload.get("attempts"), dict):
        payload.setdefault("attempt", payload["attempts"].get("round"))
    return {"type": event_type, "content": payload}


def _event_budgets(st: dict | None, *, round_no: int | None = None) -> dict:
    """Return bounded public token/tool/repair budget counters."""
    st = st or {}
    # ``tokens_used`` is cumulative for the session.  A timeline budget is
    # per chat run, so subtract the run's starting counter before publishing.
    used_tokens = max(0, int(st.get("tokens_used", 0) or 0) -
                      int(st.get("chat_token_start", 0) or 0))
    budget = int(st.get("chat_budget") or CHAT_TOKEN_BUDGET)
    remaining = max(0, budget - used_tokens)
    provider_remaining = st.get("tokens_remaining")
    if provider_remaining is not None:
        try:
            remaining = min(remaining, max(0, int(provider_remaining)))
        except (TypeError, ValueError):
            pass
    used_tools = int(st.get("tool_attempts", 0) or 0)
    current_round = round_no if round_no is not None else st.get("current_round", 0)
    repairs = int(st.get("repair_attempts", 0) or 0)
    return {
        "tokens": {"used": used_tokens, "limit": budget,
                   "remaining": max(0, int(remaining))},
        # There is no independent per-run tool-call cap; MAX_TOOL_ROUNDS is a
        # round cap, not a call cap. Do not display a fictitious call limit.
        "tools": {"used": used_tools, "limit": None, "remaining": None},
        "eda": {
            "screens": {
                "used": int(st.get("screen_attempts", 0) or 0),
                "limit": MAX_SCREEN_INVOCATIONS,
                "remaining": max(0, MAX_SCREEN_INVOCATIONS -
                                   int(st.get("screen_attempts", 0) or 0)),
            },
            "full_pnr": {
                "used": int(st.get("full_pnr_invocations", 0) or 0),
                "limit": MAX_FULL_PNR_INVOCATIONS,
                "remaining": max(0, MAX_FULL_PNR_INVOCATIONS -
                                   int(st.get("full_pnr_invocations", 0) or 0)),
            },
        },
        "rounds": {"used": int(current_round or 0), "limit": MAX_TOOL_ROUNDS,
                    "remaining": max(0, MAX_TOOL_ROUNDS - int(current_round or 0))},
        "repair": {"used": repairs, "limit": TOTAL_REPAIR_CAP,
                   "remaining": max(0, TOTAL_REPAIR_CAP - repairs)},
        "context": {
            "estimate": st.get("next_prompt_estimate"),
            "soft_limit": CONTEXT_SOFT_LIMIT,
            "hard_limit": CONTEXT_HARD_LIMIT,
            "hard_exceeded": bool(
                st.get("next_prompt_estimate") is not None and
                st.get("next_prompt_estimate") >= CONTEXT_HARD_LIMIT),
        },
    }


def _event_attempts(st: dict | None, *, round_no: int | None = None) -> dict:
    st = st or {}
    return {
        "round": int(round_no if round_no is not None else st.get("current_round", 0) or 0),
        "tool": int(st.get("tool_attempts", 0) or 0),
        "syntax": int(st.get("syntax_attempts", 0) or 0),
        "repair": int(st.get("repair_attempts", 0) or 0),
        "synthesis": int(st.get("synthesis_invocations", 0) or 0),
        "screen": int(st.get("screen_attempts", 0) or 0),
        "non_improving": int(st.get("non_improving_attempts", 0) or 0),
    }


def _tool_label(name: str, args: dict | None = None) -> tuple[str, str]:
    """Map internal names to concise public phase/action labels."""
    args = args or {}
    if name == "define_architecture":
        return "architecture", "Defining architecture"
    if name == "write_and_check_syntax":
        return "lint", "Linting"
    if name == "run_simulation":
        return "simulation", "Simulating"
    if name in _SYNTHESIS_NAMES:
        if _is_full_pnr(args, name):
            return "implementation", "Running full P&R"
        period = _as_finite_float(args.get("clock_period_ns"))
        if period and period > 0:
            return "synthesis", f"Synthesizing at {1000.0 / period:g} MHz"
        return "synthesis", "Synthesizing"
    return "tool", f"Running {name}"


def _tool_progress_event(name: str, result: str, *, started: bool = False,
                         args: dict | None = None, state: dict | None = None,
                         round_no: int | None = None) -> dict:
    phase, action = _tool_label(name, args)
    attempts = _event_attempts(state, round_no=round_no)
    budgets = _event_budgets(state, round_no=round_no)
    if started:
        return _structured_event("progress", phase, action, "running",
                                 attempts=attempts, budgets=budgets)
    failed = _recognized_tool_failure(name, result)
    status = "failed" if failed else "done"
    if name == "define_architecture":
        action = "Architecture rejected" if failed else "Architecture proposal ready"
    elif name == "write_and_check_syntax":
        action = "Lint failed" if failed else "Lint passed"
    elif name == "run_simulation":
        action = "Simulation failed" if failed else "Simulation passed"
    elif name in _SYNTHESIS_NAMES and failed:
        action = "Synthesis failed"
    else:
        action = f"{action} complete"
    return _structured_event("progress", phase, action, status,
                             evidence=_compact_result(name, result, 280),
                             attempts=attempts, budgets=budgets)


def _metric_delta(candidate: dict | None, baseline: dict | None) -> dict:
    """Compute measured candidate-baseline deltas without inventing metrics."""
    if not candidate or not baseline:
        return {}
    left, right = candidate.get("metrics") or {}, baseline.get("metrics") or {}
    delta = {}
    for key in ("setupslack", "wns_ns", "fmax_mhz", "cellarea", "totalarea",
                "power", "peakpower", "utilization", "cells"):
        a, b = _as_finite_float(left.get(key)), _as_finite_float(right.get(key))
        if a is not None and b is not None:
            delta[key] = a - b
    return delta


def _decision_event_for_result(name: str, result: str, *, state: dict | None = None,
                               round_no: int | None = None) -> dict | None:
    """Map explicit policy outcomes to public decisions, not hidden reasoning."""
    markers = (
        ("CANDIDATE PROMOTED", "promoted", "Cheap screen promoted candidate"),
        ("FULL P&R BLOCKED", "blocked", "Full P&R authorization blocked"),
        ("REVISION ACCEPTED", "accepted", "Verified revision accepted"),
        ("REVISION REJECTED", "rejected", "Verified revision rejected"),
        ("REVISION FAILED", "failed", "Verified revision failed"),
        ("REVISION NOT CHECKPOINTED", "blocked", "Revision was not checkpointed"),
        ("SIMULATION CANDIDATE FAILED", "failed", "Candidate simulation failed; restored checkpoint"),
        ("Timing retry blocked:", "blocked", "Timing retry blocked"),
        ("Timing retry stopped:", "failed", "Timing diagnosis inconclusive"),
        ("BUDGET EXHAUSTED", "blocked", "Repair budget exhausted"),
    )
    for marker, status, action in markers:
        if marker in result:
            delta = {}
            if name in _SYNTHESIS_NAMES and state:
                candidate = None
                for item in reversed(state.get("revisions") or []):
                    attempts = item.get("synthesis_attempts") or []
                    if attempts and (attempts[-1].get("result") == result or
                                      str(attempts[-1].get("result") or "") in result):
                        candidate = item
                        break
                baseline = state.get("promotion_baseline") or state.get("best_revision")
                delta = _metric_delta(candidate, baseline)
            if marker == "REVISION REJECTED" and delta.get("setupslack", 0) < 0:
                action = (f"Revision rejected: slack regressed by "
                          f"{abs(delta['setupslack']):g} ns")
            return _structured_event(
                "decision", "revision" if name in _SYNTHESIS_NAMES else "repair",
                action, status, evidence=_compact_result(name, result, 300),
                attempts=_event_attempts(state, round_no=round_no),
                budgets=_event_budgets(state, round_no=round_no),
                metric_delta=delta or None)
    return None


def _stop_decision_event(reason: str) -> dict:
    return _structured_event("decision", "run", "Run stopped", "blocked",
                             reason=reason)


def _compact_result(name: str, result: str, limit: int = 700) -> str:
    """Make a one-line-ish run-ledger item without discarding outcome words."""
    text = str(result or "").strip()
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        bits = []
        for key in ("ok", "verdict", "status", "outcome", "gate", "errors_total",
                    "warnings_total", "fmax_mhz", "setupslack", "wns_ns"):
            if key not in parsed:
                continue
            value = parsed[key]
            if key == "gate" and isinstance(value, dict):
                value = value.get("allowed", value)
            bits.append(f"{key}={value}")
        if bits:
            return _truncate(f"{name}: " + ", ".join(bits), limit)
    first_lines = [line.strip() for line in text.splitlines() if line.strip()]
    return _truncate(f"{name}: " + (" | ".join(first_lines[:3]) or "(empty result)"), limit)


def _latest_context_diagnostic(history: list[dict], name_by_id: dict[str, str]) -> str:
    latest = None
    latest_name = "tool"
    for entry in history:
        if entry.get("role") != "tool":
            continue
        content = str(entry.get("content") or "")
        name = name_by_id.get(entry.get("tool_call_id"), "tool")
        if name in {"write_and_check_syntax", "run_simulation", "run_verification",
                    "run_silicon_compiler", "find_fmax", "run_tiny_tapeout_hardening"}:
            latest, latest_name = content, name
    if latest is None:
        return "No tool diagnostics are available yet."
    return f"Latest diagnostic ({latest_name}):\n{_truncate_middle(latest, MODEL_CONTEXT_DIAGNOSTIC_MAX_CHARS)}"


def _compact_run_summary(history: list[dict], state: dict | None) -> str:
    name_by_id = _tool_call_name_map(history)
    completed = []
    for entry in history:
        if entry.get("role") == "tool":
            completed.append(_compact_result(
                name_by_id.get(entry.get("tool_call_id"), "tool"),
                str(entry.get("content") or "")))
    users = [str(e.get("content") or "") for e in history if e.get("role") == "user"]
    lines = [
        "COMPACT RUN CONTEXT (durable transcript is retained server-side)",
        f"history_entries={len(history)} user_turns={len(users)} completed_tool_results={len(completed)}",
    ]
    if users:
        lines.append("current_user_request: " + _truncate_middle(users[-1], 5000))
    if state:
        revisions = state.get("revisions") or []
        best = state.get("best_revision") or {}
        if revisions:
            lines.append(f"verified_checkpoints={len(revisions)}")
        if best:
            metrics = best.get("metrics") or {}
            lines.append(
                "best_checkpoint="
                f"r{best.get('revision', '?')} sha256={str(best.get('code_sha256', ''))[:16]} "
                f"functional={best.get('functional_passed')} synthesis={best.get('synthesis_passed')} "
                f"metrics={_truncate(json.dumps(metrics, sort_keys=True), 1200)}")
        if state.get("timing_diagnosis"):
            lines.append("timing_diagnosis=" + _truncate(
                json.dumps(state["timing_diagnosis"], sort_keys=True), 1800))
        if state.get("screen_attempts") or state.get("promoted_revision"):
            promoted = state.get("promoted_revision") or {}
            lines.append(
                "phase3_screen="
                f"attempts={state.get('screen_attempts', 0)} "
                f"passes={state.get('screen_passes', 0)} "
                f"promoted_r={promoted.get('revision', 'none')} "
                f"full_pnr={state.get('full_pnr_invocations', 0)}/{MAX_FULL_PNR_INVOCATIONS}")
        if state.get("run_stop_reason"):
            lines.append("run_stop_reason=" + _truncate(str(state["run_stop_reason"]), 1500))
    if completed:
        lines.append("recent_tool_outcomes:")
        lines.extend(f"- {item}" for item in completed[-8:])
    lines.append(_latest_context_diagnostic(history, name_by_id))
    return "\n".join(lines)


def _artifact_context(artifacts: dict[str, dict]) -> str:
    if not artifacts:
        return "CURRENT ARTIFACTS\nNo RTL or testbench has been produced yet."
    chunks = ["CURRENT ARTIFACTS (hashes identify the durable source snapshots)"]
    for label, item in artifacts.items():
        ref = f"{label}:{item['sha256'][:16]}"
        chunks.append(
            f"[{ref}] top={item['top']} chars={item['chars']} sha256={item['sha256']}\n"
            f"The complete {label} is retained in durable history; use this snapshot when repairing it:\n"
            f"```verilog\n{_truncate_middle(item['code'], MODEL_CONTEXT_ARTIFACT_MAX_CHARS)}\n```"
        )
    return "\n\n".join(chunks)


def _compact_protocol_entry(entry: dict, artifacts: dict[str, dict]) -> dict:
    """Compact tool-call arguments without changing OpenAI's tool protocol shape."""
    result = _sanitize_message(entry)
    if not result:
        return {}
    if result.get("role") != "assistant" or not result.get("tool_calls"):
        if result.get("role") == "tool":
            result["content"] = _truncate_middle(
                str(result.get("content") or ""), MODEL_CONTEXT_DIAGNOSTIC_MAX_CHARS)
        elif isinstance(result.get("content"), str):
            result["content"] = _truncate_middle(result["content"], 6000)
        return result

    compact_calls = []
    for call in result["tool_calls"]:
        compact_call = dict(call)
        fn = dict(call.get("function") or {})
        args = _call_args(call)
        if args:
            compact_args = dict(args)
            for key, label in _CONTEXT_ARTIFACT_KEYS:
                value = compact_args.get(key)
                if isinstance(value, str) and len(value) > 1200:
                    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
                    compact_args[key] = (
                        f"[artifact {label}:{digest[:16]} omitted from protocol tail; "
                        "see CURRENT ARTIFACTS]")
            fn["arguments"] = json.dumps(compact_args, separators=(",", ":"))
        compact_call["function"] = fn
        compact_calls.append(compact_call)
    result["tool_calls"] = compact_calls
    # Assistant tool-call messages conventionally omit content. If a provider
    # supplied an empty content field, dropping it keeps the wire shape clean.
    if not result.get("content"):
        result.pop("content", None)
    return result


def _context_size(messages: list[dict]) -> int:
    try:
        return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return sum(len(str(m)) for m in messages)


def _fit_model_context(messages: list[dict]) -> list[dict]:
    """Apply a hard bound without invalidating the active chat protocol.

    Historical snippets may be discarded, but the non-empty system messages,
    latest user request, and active assistant-tool exchange are protected. If
    those protected messages alone exceed the configured target, this function
    returns the still-valid (over-bound) context so admission can fail closed;
    it never emits an empty system/user message or a dangling tool result.
    """
    target = max(256, int(MODEL_CONTEXT_MAX_CHARS))
    cleaned = [_sanitize_message(entry) for entry in messages]
    cleaned = [entry for entry in cleaned if entry]
    if _context_size(cleaned) <= target:
        return cleaned

    user_indices = [i for i, entry in enumerate(cleaned)
                    if entry.get("role") == "user" and
                    bool(str(entry.get("content") or "").strip())]
    current_user = user_indices[-1] if user_indices else None
    system_indices = {i for i, entry in enumerate(cleaned)
                      if entry.get("role") in {"system", "developer"}}
    protected = set(system_indices)
    if current_user is not None:
        protected.add(current_user)

    # The active protocol begins at the most recent assistant tool call and
    # includes all following tool messages. It must stay ordered and paired.
    protocol_start = None
    for i in range(len(cleaned) - 1, -1, -1):
        if cleaned[i].get("role") == "assistant" and cleaned[i].get("tool_calls"):
            protocol_start = i
            break
    if protocol_start is not None:
        protected.update(range(protocol_start, len(cleaned)))

    def content_size(i: int) -> int:
        value = cleaned[i].get("content")
        return len(value) if isinstance(value, str) else 0

    def shrink(index: int, limit: int) -> bool:
        value = cleaned[index].get("content")
        if not isinstance(value, str) or len(value) <= limit:
            return False
        # A protected message remains non-empty. Historical optional snippets
        # may be emptied and removed below.
        minimum = 1 if index in protected else 0
        new_limit = max(minimum, limit)
        cleaned[index]["content"] = _truncate_middle(value, new_limit)
        return True

    # First discard old optional conversational payloads; this is what normally
    # resolves a large transcript without touching the current exchange.
    for i in range(len(cleaned) - 1, -1, -1):
        if i in protected or cleaned[i].get("role") not in {"user", "assistant", "tool"}:
            continue
        shrink(i, 1200)
    while _context_size(cleaned) > target:
        candidates = [(i, content_size(i)) for i in range(len(cleaned))
                      if i not in protected and content_size(i) > 0]
        if not candidates:
            break
        index, size = max(candidates, key=lambda item: item[1])
        shrink(index, max(1, size // 2))
        if content_size(index) <= 1:
            # Remove only expendable historical snippets. Never remove the
            # current user, system, or active tool protocol entries.
            cleaned.pop(index)
            protected = {i - 1 if i > index else i for i in protected if i != index}

    # If a deliberately tiny test/deployment bound still cannot fit, reduce
    # protected text to a non-empty marker while retaining its protocol role.
    while _context_size(cleaned) > target:
        candidates = [(i, content_size(i)) for i in protected if content_size(i) > 1]
        if not candidates:
            break
        index, size = max(candidates, key=lambda item: item[1])
        shrink(index, max(1, size // 2))

    return cleaned


def build_model_context(history: list[dict], state: dict | None = None) -> list[dict]:
    """Build bounded provider messages from the complete durable transcript.

    The returned list always contains the current user turn and, when the
    latest turn is awaiting another model response, the matching assistant
    ``tool_calls`` plus ``tool`` messages. This keeps the OpenAI protocol valid
    while avoiding replaying every historical source blob and raw job log.
    """
    history = list(history or [])
    system = {"role": "system", "content": SYSTEM_INSTRUCTION}
    artifacts = _latest_context_artifacts(history, state)
    dynamic = _compact_run_summary(history, state) + "\n\n" + _artifact_context(artifacts)
    frozen_context = _frozen_matmul3_context(
        (state or {}).get("current_request"),
        benchmark_contract_id=_benchmark_contract_id_for(state),
    )
    if frozen_context:
        dynamic += "\n\n" + frozen_context
    if state and state.get("continuation_instruction"):
        # Keep this out of durable history and the visible transcript. It is
        # only a one-shot provider recovery hint after finish_reason=length.
        dynamic += "\n\n" + str(state["continuation_instruction"])
    # Keep enough room for protocol messages and the current request.
    dynamic = _truncate_middle(dynamic, max(0, MODEL_CONTEXT_MAX_CHARS // 2))
    messages = [system, {"role": "system", "content": dynamic}]

    user_indices = [i for i, entry in enumerate(history) if entry.get("role") == "user"]
    current_index = user_indices[-1] if user_indices else None
    if current_index is None:
        current_index = len(history)
    prefix = []
    for entry in history[:current_index]:
        if entry.get("role") not in {"user", "assistant"}:
            continue
        if entry.get("tool_calls"):
            continue
        if not isinstance(entry.get("content"), str):
            continue
        compact = _compact_protocol_entry(entry, artifacts)
        if compact:
            prefix.append(compact)
    messages.extend(prefix[-MODEL_CONTEXT_RECENT_MESSAGES:])
    if user_indices:
        current = _sanitize_message(history[user_indices[-1]])
        current["content"] = _truncate_middle(str(current.get("content") or ""),
                                               MODEL_CONTEXT_USER_MAX_CHARS)
        messages.append(current)

    # Include only the currently active assistant/tool exchange. Older tool
    # exchanges are represented by the compact run ledger and diagnostics.
    protocol_start = None
    for i in range(current_index, len(history)):
        if history[i].get("role") == "assistant" and _entry_tool_calls(history[i]):
            protocol_start = i
            break
    if protocol_start is not None:
        for entry in history[protocol_start:]:
            if entry.get("role") in {"assistant", "tool"}:
                messages.append(_compact_protocol_entry(entry, artifacts))
    return _fit_model_context(messages)


# Private spelling retained for callers/tests that treat context assembly as an
# internal agent-core detail. Both names intentionally return the same bounded
# provider message list.
def _build_model_context(history: list[dict], state: dict | None = None) -> list[dict]:
    return build_model_context(history, state)


# -- repair policy + token accounting (Task 5) --------------------------------

def _code_diff(old: str | None, new: str, limit: int = 2000) -> str:
    """Unified diff between the previous lint attempt and the current code."""
    if old is None:
        return "(no earlier attempt recorded)"
    import difflib
    diff = "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile="previous_attempt", tofile="current_attempt", lineterm=""))
    return _truncate(diff, limit) if diff else "(identical to the previous attempt)"


def _lint_failed(result: str) -> bool:
    """A lint result counts as a failed repair attempt when the structured
    report says not-ok. Non-JSON results (no linter installed) don't count."""
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and parsed.get("ok") is False


def _normalized_usage(usage) -> dict:
    """Normalize provider usage fields for generation_usage websocket frames."""
    if usage is None:
        return {}
    def value(*names):
        for name in names:
            item = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            if item is not None:
                return item
        return None
    result = {}
    details = value("completion_tokens_details", "output_tokens_details")
    detail_reasoning = None
    if details is not None:
        detail_reasoning = (details.get("reasoning_tokens") if isinstance(details, dict)
                            else getattr(details, "reasoning_tokens", None))
    for output, names in {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
        "reasoning_tokens": ("reasoning_tokens",),
    }.items():
        item = detail_reasoning if output == "reasoning_tokens" and detail_reasoning is not None else value(*names)
        if item is not None:
            try:
                result[output] = max(0, int(item))
            except (TypeError, ValueError):
                continue
    if "total_tokens" not in result and {"input_tokens", "output_tokens"} <= result.keys():
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def _record_usage(session_id: str, usage, *, generation_id: str | None = None,
                  run_id: str | None = None, event_id: str | None = None) -> dict:
    """Record provider usage exactly once and return the accounting verdict.

    Prompt estimates remain outside this path.  ``generation_id`` makes a
    replayed final usage frame idempotent; callers with multiple frames may
    provide an explicit event id.
    """
    st = _get_state(session_id)
    ledger = UsageLedger.from_snapshot(st.get("usage_ledger"))
    result = ledger.record(usage, generation_id=generation_id, run_id=run_id,
                           event_id=event_id)
    st["usage_ledger"] = ledger.snapshot()
    totals = ledger.project_totals
    st["api_prompt_tokens"] = totals["input_tokens"]
    st["api_completion_tokens"] = totals["output_tokens"]
    st["api_total_tokens"] = totals["total_tokens"]
    st["tokens_used"] = totals["total_tokens"]
    st["tokens_remaining"] = ledger.last_tokens_remaining
    # Usage is part of the durable project accounting, not just a live UI
    # counter. Persist after each provider response so a restart cannot erase
    # a completed generation's spend. The repository save is atomic and is a
    # no-op for transient non-project sessions.
    _save_project(session_id)
    return result


def _stable_token_payload(value) -> bytes:
    """Serialize a value deterministically for provider-independent estimates."""
    if isinstance(value, str):
        payload = value
    else:
        payload = json.dumps(value, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), default=str)
    return payload.encode("utf-8")


def estimate_tokens(value, *, bytes_per_token: int | None = None) -> int:
    """Return a deterministic conservative token estimate for *value*."""
    divisor = bytes_per_token or TOKEN_ESTIMATE_BYTES_PER_TOKEN
    if divisor <= 0:
        divisor = 4
    size = len(_stable_token_payload(value))
    return (size + divisor - 1) // divisor if size else 0


def estimate_prompt_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    """Estimate input tokens for one chat-completions request.

    The serialized request includes tool schemas, which are part of the model
    context even though they are not present in the visible message history.
    """
    request = {"messages": messages, "tools": tools or []}
    # Fixed framing keeps the estimate conservative and reproducible.
    return estimate_tokens(request) + 4


# Private aliases keep the helper seam easy to probe without implying that an
# estimate is authoritative provider usage.
_estimate_tokens = estimate_tokens
_estimate_prompt_tokens = estimate_prompt_tokens


def session_api_usage(session_id: str) -> dict[str, int | None]:
    """Return cumulative API usage, explicitly excluding prompt estimates."""
    st = _get_state(session_id)
    ledger = UsageLedger.from_snapshot(st.get("usage_ledger"))
    totals = ledger.project_totals
    return {
        "prompt_tokens": totals["input_tokens"],
        "completion_tokens": totals["output_tokens"],
        "total_tokens": totals["total_tokens"],
        "tokens_remaining": ledger.last_tokens_remaining,
    }


def _context_preflight(session_id: str, messages: list[dict],
                       tools: list[dict] | None = None) -> dict:
    """Compute admission with explicit input and completion allowances."""
    estimate = estimate_prompt_tokens(messages, tools)
    st = _get_state(session_id)
    st["next_prompt_estimate"] = estimate
    run_used = _chat_tokens_used(st)
    # Effort-scaled run budget, pinned at run start (_begin_chat_run).
    budget = int(st.get("chat_budget") or CHAT_TOKEN_BUDGET)
    run_remaining = max(0, budget - run_used)
    # Reasoning models bill thinking against the completion budget, so their
    # per-request ceiling must leave room for it (see REASONING_OUTPUT_TOKEN_LIMIT).
    output_cap = (REASONING_OUTPUT_TOKEN_LIMIT
                  if _session_model_reasoning(session_id)
                  else MODEL_OUTPUT_TOKEN_LIMIT)
    output_allowance = min(
        output_cap, max(0, run_remaining - estimate))
    envelope = estimate + output_allowance
    return {
        "estimate": estimate,
        "output_allowance": output_allowance,
        "output_cap": output_cap,
        "envelope": envelope,
        "soft_limit": CONTEXT_SOFT_LIMIT,
        "hard_limit": CONTEXT_HARD_LIMIT,
        "soft_warning": envelope >= CONTEXT_SOFT_LIMIT,
        "hard_exceeded": envelope > CONTEXT_HARD_LIMIT,
        "run_used": run_used,
        "run_remaining": run_remaining,
        "run_exceeded": estimate >= run_remaining or output_allowance <= 0,
        "session_api_usage": session_api_usage(session_id),
    }


_SIM_FAILURES = ("SIMULATION FAILED", "SIMULATION INCONCLUSIVE",
                 "Simulation compile FAILED", "Simulation TIMED OUT")

_SYNTHESIS_NAMES = frozenset({"run_silicon_compiler", "find_fmax"})


def _synthesis_flow(args: dict | None, name: str = "") -> str:
    """Classify an implementation request without trusting model prose.

    ``find_fmax`` is always a screen (it intentionally runs synthesis + STA).
    For ``run_silicon_compiler`` the optional flow/stage aliases make this
    compatible with both the current EDA adapter and the Phase 3 adapter. A
    missing flow is deliberately treated as the cheap screen; dispatch adds
    ``syn-sta`` when the installed adapter accepts that keyword.
    """
    if name == "find_fmax":
        return "screen"
    args = args or {}
    raw = (args.get("flow") or args.get("implementation_flow") or
           args.get("stage") or args.get("run_kind"))
    if isinstance(raw, bool):
        return "full_pnr" if raw else "screen"
    value = str(raw or "").strip().lower().replace(" ", "-")
    if args.get("full_pnr") is True or args.get("run_pnr") is True:
        return "full_pnr"
    if value in _FULL_PNR_FLOW_NAMES or "place" in value or "route" in value:
        return "full_pnr"
    return "screen"


def _is_full_pnr(args: dict | None, name: str = "") -> bool:
    return _synthesis_flow(args, name) == "full_pnr"


_SETUPSLACK_RE = re.compile(
    r"(?im)^\s*\"?\s*(?:setupslack|wns_ns)\s*\"?\s*:\s*"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)")


def _canonical_tool_call(name: str, args: dict) -> str:
    return json.dumps({"name": name, "args": args},
                      sort_keys=True, separators=(",", ":"))


def _recognized_tool_failure(name: str, result: str) -> bool:
    """Return true only for an outcome that merits a deliberate retry."""
    if "SAVED SOURCE REFERENCE BLOCKED:" in (result or ""):
        return True
    if name == "write_and_check_syntax":
        return _lint_failed(result)
    if name == "run_simulation":
        return any(marker in result for marker in _SIM_FAILURES)
    if name in _SYNTHESIS_NAMES:
        if re.search(r"(?i)\b(?:synthesis|find_fmax) failed\b|\btool error\b", result):
            return True
        match = _SETUPSLACK_RE.search(result)
        return bool(match and float(match.group(1)) < 0)
    return bool(re.search(r"(?i)\b(?:tool error|bad tool arguments)\b", result))


def _synthesis_timing_violation(result: str) -> float | None:
    match = _SETUPSLACK_RE.search(result)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if value < 0 else None


# -- revision checkpoints + measured optimization ----------------------------

_REVISION_NUMBER_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_REVISION_METRIC_KEYS = ("cellarea", "totalarea", "utilization", "cells", "power",
                         "peakpower", "setupslack", "holdslack", "wns_ns", "fmax_mhz",
                         "chosen_period_ns")


def _code_sha256(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


_LINEAGE_SCHEMA = "candidate-lineage/v1"
_LINEAGE_SNAPSHOT_FIELDS = {
    "candidate_id", "candidate_revision", "revision", "code_sha256",
    "verilog_code", "top_module", "architecture_contract",
    "architecture_contract_hash", "external_contract_hash", "testbench_code",
    "testbench_sha256", "reference_sha256", "functional_passed",
    "functional_checkpoint_id", "synthesis_passed", "metrics",
    "setupslack_ns", "measurement_id", "measurement_binding",
    "measurement_stage", "target", "constraints_sha256", "toolchain_identity",
    "job_id", "rationale", "status", "synthesis_attempts",
    "run_id", "rtl_artifact_sha256", "testbench_artifact_sha256",
    "promotion_token", "promotion_binding", "screen_evidence_sha256",
}


def _persist_candidate_source(code: str, *, name: str, kind: str) -> str:
    """Store one candidate source blob and verify it is immediately readable."""
    ref = _evidence_repository.put_artifact(
        code.encode("utf-8"), name=name, media_type="text/x-systemverilog",
        metadata={"kind": kind, "producer": "agent_candidate_checkpoint"})
    restored = _evidence_repository.get_artifact(ref.sha256)
    if restored != code.encode("utf-8"):
        raise EvidenceError(f"{kind} artifact failed read-after-write verification")
    return ref.sha256


def _restore_candidate_source(snapshot: dict, *, field: str,
                              artifact_field: str, required: bool) -> str | None:
    """Resolve and hash-check source; an existing artifact ref never falls back."""
    digest = snapshot.get(artifact_field)
    inline = snapshot.get(field)
    if isinstance(digest, str):
        try:
            content = _evidence_repository.get_artifact(digest)
        except EvidenceError:
            return None
        if content is None or hashlib.sha256(content).hexdigest() != digest:
            return None
        try:
            restored = content.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if isinstance(inline, str) and inline and inline != restored:
            return None
        return restored
    if not isinstance(inline, str) or not inline or _looks_like_artifact_handle(inline):
        return None if required else ""
    try:
        stored = _persist_candidate_source(
            inline, name=("candidate.sv" if field == "verilog_code" else "testbench.sv"),
            kind=("rtl" if field == "verilog_code" else "testbench"))
    except (EvidenceError, OSError):
        return None
    snapshot[artifact_field] = stored
    return inline


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _external_contract_hash(state: dict) -> str | None:
    """Bind evidence to user-visible behavior, excluding internal strategy."""
    architecture = state.get("architecture_contract")
    if not isinstance(architecture, dict):
        return None
    external = {key: architecture.get(key) for key in (
        "schema_version", "target_type", "top_module", "technology",
        "clock_target_mhz", "clock_period_ns", "latency_cycles",
        "throughput_per_cycle",
    )}
    return _sha256_json(external)


def _toolchain_identity(result: str) -> str:
    """Return a conservative identity; unknown tools compare only with unknown."""
    lines = []
    for line in str(result or "").splitlines():
        if re.search(r"(?i)\b(?:toolchain|yosys|opensta|openroad|siliconcompiler|verilator)\b.*\b(?:version|v?\d)", line):
            lines.append(line.strip()[:240])
    return _sha256_json(lines or ["unspecified-toolchain"])


def _measurement_context(args: dict, checkpoint: dict, result: str,
                         tool_name: str, stage: str) -> dict:
    constraints = args.get("constraints")
    constraints_hash = _sha256_json(constraints) if constraints is not None else _sha256_json({
        "clock_period_ns": args.get("clock_period_ns"),
        "target": args.get("target", "skywater130_demo"),
    })
    context = {
        "rtl_sha256": checkpoint.get("code_sha256"),
        "testbench_sha256": checkpoint.get("testbench_sha256"),
        "reference_sha256": checkpoint.get("reference_sha256"),
        "external_contract_hash": checkpoint.get("external_contract_hash"),
        "target": args.get("target", "skywater130_demo"),
        "constraints_sha256": constraints_hash,
        "measurement_stage": stage,
        "tool": tool_name,
        "toolchain_identity": _toolchain_identity(result),
    }
    context["measurement_binding"] = _sha256_json(context)
    return context


def _measurement_compatible(left: dict | None, right: dict | None) -> bool:
    """Only like-for-like implementation evidence may select a winner."""
    if not left or not right:
        return True
    keys = ("testbench_sha256", "reference_sha256", "external_contract_hash",
            "target", "constraints_sha256", "measurement_stage",
            "toolchain_identity")
    return all(left.get(key) == right.get(key) for key in keys)


def _restore_active_candidate(state: dict, snapshot: dict | None) -> None:
    """Restore source selection and compatible internal architecture together."""
    if not isinstance(snapshot, dict):
        return
    state["active_revision"] = dict(snapshot)
    state["last_good_verilog"] = snapshot.get("verilog_code")
    state["setupslack_ns"] = snapshot.get("setupslack_ns")
    architecture = snapshot.get("architecture_contract")
    architecture_hash = snapshot.get("architecture_contract_hash")
    if not isinstance(architecture, dict) or not isinstance(architecture_hash, str):
        return
    state["architecture_contract"] = dict(architecture)
    state["architecture_contract_hash"] = architecture_hash
    surface = state.get("design_contract")
    if not isinstance(surface, dict):
        return
    try:
        locked = _contract_state_from(surface)
        if locked.status != "locked":
            return
        previous = locked.canonical()
        spec = locked.spec.model_copy(deep=True)
        metadata = dict(spec.metadata or {})
        metadata["architecture_contract"] = dict(architecture)
        spec.metadata = metadata
        goals = dict(spec.timing_goals or {})
        goals["pipeline_stages"] = architecture.get("pipeline_stages")
        spec.timing_goals = goals
        locked.spec = spec
        locked.revision += 1
        locked.digest = locked.recompute_digest()
        state["design_contract"] = locked.canonical()
        state["contract_revision_diff"] = _contract_revision_diff(
            previous, state["design_contract"])
        state["contract_run_revision"] = locked.revision
        state["contract_run_digest"] = locked.digest
    except (ContractProtocolError, ValueError, TypeError):
        logger.warning("could not restore architecture metadata for candidate %s",
                       snapshot.get("candidate_id"))


def _lineage_snapshot(snapshot: dict | None) -> dict | None:
    if not isinstance(snapshot, dict):
        return None
    clean = {key: snapshot[key] for key in _LINEAGE_SNAPSHOT_FIELDS if key in snapshot}
    code = _restore_candidate_source(
        clean, field="verilog_code", artifact_field="rtl_artifact_sha256",
        required=True)
    digest = clean.get("code_sha256")
    if (not isinstance(code, str) or not code or _looks_like_artifact_handle(code)
            or digest != _code_sha256(code)):
        return None
    clean["verilog_code"] = code
    testbench = _restore_candidate_source(
        clean, field="testbench_code",
        artifact_field="testbench_artifact_sha256",
        required=bool(clean.get("functional_passed")))
    if testbench is None:
        return None
    if testbench:
        if clean.get("testbench_sha256") != _code_sha256(testbench):
            return None
        clean["testbench_code"] = testbench
    clean["candidate_id"] = clean.get("candidate_id") or f"cand-{digest[:16]}"
    return clean


def _serialize_candidate_lineage(state: dict) -> dict | None:
    revisions = [item for item in (_lineage_snapshot(value) for value in state.get("revisions", [])) if item]
    best = _lineage_snapshot(state.get("best_revision"))
    active = _lineage_snapshot(state.get("active_revision")) or best
    latest = _lineage_snapshot(state.get("latest_attempted_revision"))
    if not revisions and not best and not latest:
        return None
    promoted = _lineage_snapshot(state.get("promoted_revision"))
    baseline = _lineage_snapshot(state.get("promotion_baseline"))
    return {"schema_version": _LINEAGE_SCHEMA, "revisions": revisions,
            "best": best, "active": active, "latest_attempt": latest,
            "promoted": promoted, "promotion_baseline": baseline}


def _validated_candidate_lineage(value: object) -> dict | None:
    if not isinstance(value, dict) or value.get("schema_version") != _LINEAGE_SCHEMA:
        return None
    revisions = [item for item in (_lineage_snapshot(v) for v in value.get("revisions", [])) if item]
    by_hash = {item["code_sha256"]: item for item in revisions}
    def member(name: str):
        item = _lineage_snapshot(value.get(name))
        return by_hash.get(item.get("code_sha256")) if item else None
    return {"schema_version": _LINEAGE_SCHEMA, "revisions": revisions,
            "best": member("best"), "active": member("active"),
            "latest_attempt": member("latest_attempt"),
            "promoted": member("promoted"),
            "promotion_baseline": member("promotion_baseline")}


def _durable_sim_checkpoint(history: list[dict]) -> dict | None:
    """The last RTL whose run_simulation PASSED in the persisted history.

    Restored on project load so the admission gate and the CURRENT ARTIFACTS
    context keep pointing at the verified source after a backend restart,
    even for projects whose persisted lineage has no active member (staged
    screens never became the active dashboard checkpoint).
    """
    calls: dict[str, tuple[str, dict]] = {}
    passing = None
    for entry in history or []:
        if entry.get("role") == "assistant":
            for call in entry.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    args = {}
                if isinstance(args, dict) and call.get("id"):
                    calls[str(call["id"])] = (str(fn.get("name") or ""), args)
        elif entry.get("role") == "tool":
            item = calls.get(str(entry.get("tool_call_id")))
            if (item and item[0] == "run_simulation"
                    and "SIMULATION PASSED" in str(entry.get("content") or "")):
                resolved, error = _resolve_artifact_handles_from_history(history, item[1])
                if (error is None and isinstance(resolved.get("verilog_code"), str)
                        and resolved["verilog_code"].strip()):
                    passing = {"code": resolved["verilog_code"],
                               "top_module": resolved.get("top_module") or "top"}
    return passing


def _restore_candidate_lineage(state: dict, lineage: dict) -> None:
    revisions = [dict(item) for item in lineage.get("revisions", [])]
    state["revisions"] = revisions
    state["revision_by_hash"] = {item["code_sha256"]: item for item in revisions}
    for source, target in (("best", "best_revision"), ("active", "active_revision"),
                           ("latest_attempt", "latest_attempted_revision"),
                           ("promoted", "promoted_revision"),
                           ("promotion_baseline", "promotion_baseline")):
        item = lineage.get(source)
        state[target] = state["revision_by_hash"].get(item.get("code_sha256")) if item else None
    active = state.get("active_revision")
    if active:
        state["last_good_verilog"] = active.get("verilog_code")
        state["setupslack_ns"] = active.get("setupslack_ns")


def _public_candidate_lineage(lineage: dict) -> dict:
    def summary(item):
        if not item:
            return None
        return {key: item.get(key) for key in (
            "candidate_id", "candidate_revision", "code_sha256", "status",
            "functional_checkpoint_id", "measurement_id", "measurement_stage",
            "job_id", "metrics")}
    return {"schema_version": lineage["schema_version"],
            "active": summary(lineage.get("active")),
            "best": summary(lineage.get("best")),
            "latest_attempt": summary(lineage.get("latest_attempt"))}


def _promotion_binding(args: dict, checkpoint: dict | None = None) -> str:
    """Opaque binding for one candidate's implementation context.

    RTL alone is insufficient: changing the target, clock, constraints, or
    testbench must force a fresh screen. Canonical JSON keeps this stable and
    avoids trusting a model-provided promotion label.
    """
    checkpoint = checkpoint or {}
    payload = {
        "rtl_sha256": _code_sha256(str(args.get("verilog_code", ""))),
        "testbench_sha256": _code_sha256(str(args.get("testbench_code") or
                                              checkpoint.get("testbench_code") or "")),
        "top_module": args.get("top_module") or checkpoint.get("top_module"),
        "target": args.get("target", "skywater130_demo"),
        "clock_period_ns": args.get("clock_period_ns"),
        "constraints": args.get("constraints"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _as_finite_float(value) -> float | None:
    try:
        number = float(str(value).split()[0])
    except (TypeError, ValueError, IndexError):
        return None
    return number if math.isfinite(number) else None


def _revision_metrics(result: str) -> dict:
    """Extract comparable timing/PPA values from synthesis or Fmax output.

    SiliconCompiler synthesis returns ``key: value`` lines while find_fmax
    starts with a JSON object.  Keep this parser deliberately conservative:
    missing evidence remains missing rather than being guessed as zero.
    """
    metrics: dict[str, object] = {}
    first = (result or "").lstrip().splitlines()
    if first:
        try:
            payload = json.loads(first[0])
        except (TypeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            for key in ("status", "fmax_mhz", "chosen_period_ns", "wns_ns"):
                if key in payload:
                    metrics[key] = payload[key]
            # A future structured synthesis result may put PPA below metrics.
            source = payload.get("metrics")
            if isinstance(source, dict):
                for key in _REVISION_METRIC_KEYS:
                    if key in source:
                        metrics[key] = source[key]
    for key in _REVISION_METRIC_KEYS:
        match = re.search(rf"(?im)^\s*{re.escape(key)}\s*:\s*({_REVISION_NUMBER_RE})", result or "")
        if match:
            metrics[key] = match.group(1)
    for key in list(metrics):
        if key not in ("status",):
            value = _as_finite_float(metrics[key])
            if value is not None:
                metrics[key] = value
    if "setupslack" not in metrics and "wns_ns" in metrics:
        metrics["setupslack"] = metrics["wns_ns"]
    if "fmax_mhz" not in metrics and "chosen_period_ns" in metrics:
        period = _as_finite_float(metrics["chosen_period_ns"])
        if period and period > 0:
            metrics["fmax_mhz"] = 1000.0 / period
    metrics["timing_known"] = any(k in metrics for k in ("setupslack", "fmax_mhz"))
    slack = _as_finite_float(metrics.get("setupslack"))
    metrics["timing_feasible"] = slack >= 0 if slack is not None else metrics.get("status") == "success"
    return metrics


def _revision_score(snapshot: dict) -> tuple:
    """Return a deterministic correctness-first comparison key.

    Functional simulation and a successful synthesis dominate timing evidence;
    timing feasibility/slack and Fmax dominate area/power.  ``-inf`` marks an
    unknown metric so absent data can never beat measured data by accident.
    """
    synthesis_ok = bool(snapshot.get("synthesis_passed"))
    functional_ok = bool(snapshot.get("functional_passed"))
    metrics = snapshot.get("metrics") or {}
    def value(*keys):
        for key in keys:
            number = _as_finite_float(metrics.get(key))
            if number is not None:
                return number
        return -math.inf
    return (
        int(functional_ok),
        int(synthesis_ok),
        int(bool(metrics.get("timing_known"))),
        int(bool(metrics.get("timing_feasible"))),
        value("setupslack", "wns_ns"),
        value("fmax_mhz"),
        (lambda number: -number if number != -math.inf else -math.inf)(value("cellarea", "totalarea")),
        (lambda number: -number if number != -math.inf else -math.inf)(value("power", "peakpower")),
    )


def _revision_failure(result: str) -> bool:
    pattern = (r'(?i)\b(?:synthesis|find_fmax) failed\b|\btool error\b|'
               r'\b(?:status\s*[:=]\s*[\"\']?(?:failure|inconclusive))\b')
    return bool(re.search(pattern, result or ""))


def _timing_infrastructure_failure(result: str) -> bool:
    """Recognize failures that prevented EDA evidence from being produced."""
    markers = (
        "tools did not meet version requirements",
        "executable for synthesis",
        "version check failed for",
        "no such file or directory",
        "permission denied",
        "tool error:",
        "docker daemon is not running",
    )
    lowered = (result or "").lower()
    return any(marker in lowered for marker in markers)


def _simulation_revision(session_id: str, args: dict, result: str) -> bool:
    """Create/update the verified checkpoint for a passing simulation."""
    code = args.get("verilog_code")
    if not isinstance(code, str) or "SIMULATION PASSED" not in result:
        return False
    st = _get_state(session_id)
    digest = _code_sha256(code)
    architecture_hash = st.get("architecture_contract_hash")
    testbench = str(args.get("testbench_code") or "")
    reference = args.get("reference_model") or args.get("reference_model_code") or ""
    snapshot = st["revision_by_hash"].get(digest)
    try:
        rtl_artifact_sha256 = _persist_candidate_source(
            code, name=f"{args.get('top_module') or 'top'}.sv", kind="rtl")
        testbench_artifact_sha256 = _persist_candidate_source(
            testbench, name=f"{args.get('top_module') or 'top'}_tb.sv",
            kind="testbench")
    except (EvidenceError, OSError) as exc:
        logger.error("candidate checkpoint persistence failed: %s", exc,
                     extra={"session": session_id})
        st["run_stop_reason"] = "artifact_persistence_failed"
        return False
    if snapshot is None:
        candidate_revision = len(st["revisions"]) + 1
        snapshot = {
            "revision": candidate_revision,
            "candidate_revision": candidate_revision,
            "candidate_id": f"cand-{digest[:16]}",
            "run_id": st.get("controller_run_id"),
            "code_sha256": digest,
            "external_contract_hash": _external_contract_hash(st),
            "architecture_contract_hash": architecture_hash,
            "architecture_contract": dict(st.get("architecture_contract") or {}),
            "top_module": args.get("top_module") or "top",
            "verilog_code": code,
            "rtl_artifact_sha256": rtl_artifact_sha256,
            "testbench_code": args.get("testbench_code"),
            "testbench_artifact_sha256": testbench_artifact_sha256,
            "testbench_sha256": _code_sha256(testbench),
            "reference_sha256": _code_sha256(str(reference)),
            "functional_checkpoint_id": _sha256_json({
                "rtl": digest, "testbench": _code_sha256(testbench),
                "reference": _code_sha256(str(reference)),
                "contract": _external_contract_hash(st),
            }),
            "functional_passed": True,
            "synthesis_passed": False,
            "latency": args.get("latency", args.get("latency_cycles")),
            "throughput": args.get("throughput"),
            "metrics": {},
            "setupslack_ns": None,
            "synthesis_attempts": [],
            "rationale": "simulation passed",
            "status": "functionally_verified",
        }
        st["revision_by_hash"][digest] = snapshot
        st["revisions"].append(snapshot)
    else:
        snapshot.update({"functional_passed": True,
                         "architecture_contract_hash": architecture_hash,
                         "architecture_contract": dict(st.get("architecture_contract") or {}),
                         "external_contract_hash": _external_contract_hash(st),
                         "testbench_code": args.get("testbench_code"),
                         "rtl_artifact_sha256": rtl_artifact_sha256,
                         "testbench_artifact_sha256": testbench_artifact_sha256,
                         "testbench_sha256": _code_sha256(testbench),
                         "reference_sha256": _code_sha256(str(reference)),
                         "latency": args.get("latency", args.get("latency_cycles")),
                         "throughput": args.get("throughput"),
                         "rationale": args.get("rationale") or "simulation passed",
                         "status": "functionally_verified"})
    snapshot["simulation_result"] = result
    st["current_revision"] = snapshot
    st["last_good_verilog"] = code
    return True


def _restore_simulation_checkpoints(session_id: str, history: list[dict]) -> None:
    """Rebuild verified RTL checkpoints from durable passing tool exchanges.

    The model's prose is not evidence: a checkpoint is restored only when a
    ``run_simulation`` assistant call is paired with a tool result containing
    the simulator's explicit pass marker and its exact source can be recovered
    from the durable transcript.
    """
    calls: dict[str, tuple[str, dict]] = {}
    for entry in history or []:
        if entry.get("role") == "assistant":
            for call in _entry_tool_calls(entry):
                call_id = call.get("id")
                fn = call.get("function") or {}
                name = fn.get("name") if isinstance(fn, dict) else None
                if call_id and name:
                    calls[str(call_id)] = (str(name), _call_args(call))
        elif entry.get("role") == "tool":
            item = calls.get(str(entry.get("tool_call_id")))
            if not item or item[0] != "run_simulation":
                continue
            result = str(entry.get("content") or "")
            if "SIMULATION PASSED" not in result:
                continue
            expected_contract = _get_state(session_id).get("architecture_contract_hash")
            if expected_contract:
                marker = re.search(r"(?im)^architecture_contract_sha256:\s*([0-9a-f]{64})\s*$", result)
                if not marker or marker.group(1) != expected_contract:
                    continue
            args, error = _resolve_artifact_handles_from_history(history, item[1])
            if error is None:
                _simulation_revision(session_id, args, result)


def _update_timing_diagnosis(st: dict, result: str, metrics: dict, code_sha256: str | None = None) -> None:
    """Retain timing evidence, classify the path, and constrain the next retry."""
    diagnosis: dict = {}
    json_match = re.search(r"(?im)^\s*timing_diagnosis\s*:\s*(\{.*\})\s*$", result or "")
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
            if isinstance(parsed, dict):
                diagnosis.update(parsed)
        except json.JSONDecodeError:
            pass
    prior = st.get("timing_diagnosis") or {}
    # Timing evidence is bound to the exact RTL hash. Never let a path from a
    # previous candidate steer a different candidate whose report omitted it.
    if prior and (code_sha256 is None or prior.get("code_sha256") in {None, code_sha256}):
        diagnosis = {**prior, **diagnosis}
    if code_sha256 is not None:
        diagnosis["code_sha256"] = code_sha256
    path_match = re.search(r"(?im)^\s*critical_path\s*:\s*(.+?)\s*$", result or "")
    path = path_match.group(1).strip() if path_match else diagnosis.get("critical_path")
    slack = metrics.get("setupslack")
    if slack is None:
        slack = diagnosis.get("setup_slack_ns", diagnosis.get("wns_ns"))
    if slack is not None:
        diagnosis["setup_slack_ns"] = slack
        if metrics.get("setupslack") is None:
            try:
                metrics["setupslack"] = float(slack)
            except (TypeError, ValueError):
                pass
    if path and not diagnosis.get("paths"):
        startpoint, sep, endpoint = path.partition("->")
        diagnosis["critical_path"] = path
        diagnosis["paths"] = [{"path": path, "startpoint": startpoint.strip() if sep else path,
                                "endpoint": endpoint.strip() if sep else None,
                                "slack_ns": slack}]
    contract = _architecture_contract_for(st)
    target_mhz = contract.clock_target_mhz if contract is not None else None
    fmax = _as_finite_float(metrics.get("fmax_mhz"))
    target_failure = (target_mhz is not None and fmax is not None and
                      fmax + 1e-6 < target_mhz)
    if slack is None and not diagnosis.get("paths") and not target_failure:
        return
    classification = classify_timing_path(diagnosis)
    diagnosis["critical_path_classification"] = classification
    st["timing_diagnosis"] = diagnosis
    try:
        negative = (slack is not None and float(slack) < 0) or target_failure
    except (TypeError, ValueError):
        negative = target_failure
    if negative:
        st["retry_constraint"] = {
            "critical_path": path,
            "category": classification["category"],
            "classification": classification,
            "target_mhz": target_mhz,
            "measured_fmax_mhz": fmax,
            "instruction": (
                "apply one of the recommended evidence-supported strategies: "
                + ", ".join(classification["recommended_strategies"])
            ),
            "evidence_required": not bool(diagnosis.get("paths")),
        }
    else:
        st["retry_constraint"] = None


def _synthesis_revision(session_id: str, args: dict, result: str, tool_name: str = "run_silicon_compiler") -> tuple[str, dict | None]:
    """Record a measured candidate using the Phase 3 screen/P&R policy.

    Every candidate is simulation-verified before admission (the caller's
    guard enforces that). A synthesis+STA result is only a cheap screen. It
    may promote a candidate, but only a subsequent, explicitly authorized
    full-P&R result can replace the baseline after promotion. This keeps the
    decision deterministic and makes a failed P&R recoverable.
    """
    code = args.get("verilog_code")
    if not isinstance(code, str):
        return "untracked", None
    st = _get_state(session_id)
    digest = _code_sha256(code)
    prior = st["revision_by_hash"].get(digest)
    if prior is None:
        return "unverified", None

    phase = _synthesis_flow(args, tool_name)
    staged = bool(args.get("flow") or args.get("_controller_flow") or args.get("full_pnr"))
    # A full-P&R postprocess without an admission record is fail-closed. The
    # regular chat path sets this immediately before starting the expensive job.
    if phase == "full_pnr":
        promoted = st.get("promoted_revision") or {}
        binding = _promotion_binding(args, prior)
        if (promoted.get("code_sha256") != digest or
                promoted.get("promotion_binding") != binding or
                st.get("full_pnr_active_hash") != digest):
            return "unverified", None

    candidate = dict(prior)
    measurement = _measurement_context(args, prior, result, tool_name, phase)
    candidate.update(measurement)
    candidate["measurement_id"] = f"measure-{measurement['measurement_binding'][:16]}"
    job_match = re.search(r"(?im)^job_id:\s*([^\s]+)", result or "")
    candidate["job_id"] = job_match.group(1) if job_match else None
    candidate["metrics"] = {**(prior.get("metrics") or {}), **_revision_metrics(result)}
    candidate["setupslack_ns"] = candidate["metrics"].get("setupslack")
    contract = _architecture_contract_for(st)
    measured_fmax = _as_finite_float(candidate["metrics"].get("fmax_mhz"))
    target_mhz = contract.clock_target_mhz if contract is not None else None
    target_miss = (target_mhz is not None and measured_fmax is not None and
                   measured_fmax + 1e-6 < target_mhz)
    if target_mhz is not None and measured_fmax is not None:
        candidate["metrics"]["timing_target_met"] = not target_miss
        if target_miss:
            # A positive slack at a different/implicit constraint must not
            # masquerade as meeting the approved frequency target.
            candidate["metrics"]["timing_feasible"] = False
    candidate["synthesis_passed"] = not _revision_failure(result)
    candidate["status"] = "measured" if candidate["synthesis_passed"] else "measurement_failed"
    candidate["rationale"] = args.get("rationale") or prior.get("rationale")
    candidate["synthesis_result"] = result
    _update_timing_diagnosis(st, result, candidate["metrics"], digest)
    attempt = {
        "phase": phase,
        "result": result,
        "metrics": dict(candidate["metrics"]),
        "synthesis_passed": candidate["synthesis_passed"],
    }
    candidate["synthesis_attempts"] = list(prior.get("synthesis_attempts") or []) + [attempt]
    if phase == "screen":
        candidate["screen_passed"] = candidate["synthesis_passed"]
        candidate["screen_result"] = result
    else:
        candidate["pnr_passed"] = candidate["synthesis_passed"]
        candidate["pnr_result"] = result

    st["revision_by_hash"][digest] = candidate
    index = next((i for i, item in enumerate(st["revisions"])
                  if item.get("code_sha256") == digest), None)
    if index is not None:
        st["revisions"][index] = candidate
    st["latest_attempted_revision"] = dict(candidate)

    if phase == "screen":
        if not candidate.get("synthesis_passed"):
            return "failed", candidate
        st["screen_passes"] += 1
        # Explicit syn-sta screens are provisional and must be compared with
        # the best screen seen in this run. best_revision deliberately remains
        # unchanged until full P&R confirms the promoted candidate, so using it
        # here would make every staged screen look like an improvement.
        best = (st.get("screen_best_revision") if staged else None)
        if best is None:
            best = st.get("best_revision")
        compatible = _measurement_compatible(candidate, best)
        improves = (best is None or (compatible and digest == best.get("code_sha256")) or
                    (compatible and _revision_score(candidate) > _revision_score(best)))
        if not improves:
            candidate["status"] = "rejected_incompatible" if not compatible else "rejected_regression"
            st["revision_by_hash"][digest] = candidate
            if index is not None:
                st["revisions"][index] = candidate
            st["latest_attempted_revision"] = dict(candidate)
            _restore_active_candidate(st, best)
            st["non_improving_attempts"] += 1
            return "rejected", candidate
        if staged:
            if st.get("promotion_baseline") is None and st.get("best_revision") is not None:
                st["promotion_baseline"] = dict(st["best_revision"])
            st["screen_best_revision"] = dict(candidate)
        elif best is not None and digest != best.get("code_sha256"):
            st["promotion_baseline"] = dict(best)
        elif best is None:
            st["promotion_baseline"] = None
        candidate["screen_provisional"] = staged
        candidate["promotion_binding"] = _promotion_binding(args, prior)
        candidate["screen_evidence_sha256"] = hashlib.sha256(
            str(result).encode("utf-8")
        ).hexdigest()
        candidate["promotion_token"] = hashlib.sha256(
            (candidate["promotion_binding"] + "\0" +
             candidate["screen_evidence_sha256"] + "\0" + digest).encode("utf-8")
        ).hexdigest()[:24]
        st["promoted_revision"] = dict(candidate)
        candidate["status"] = "promoted" if staged else "best"
        # Explicit Phase 3 screens are provisional. They never replace the
        # active/best dashboard checkpoint until full P&R confirms them.
        if not staged:
            st["best_revision"] = dict(candidate)
            st["active_revision"] = dict(candidate)
            st["setupslack_ns"] = candidate.get("setupslack_ns")
        st["non_improving_attempts"] = 0
        return "promoted", candidate

    # Full P&R is evaluated against the checkpoint that existed before the
    # screen promotion, not against its own cheap-screen result.
    baseline = st.get("promotion_baseline")
    if not candidate.get("synthesis_passed"):
        if baseline is not None:
            st["best_revision"] = dict(baseline)
            _restore_active_candidate(st, baseline)
        st["promoted_revision"] = None
        st["promotion_baseline"] = None
        st["screen_best_revision"] = None
        return "failed", candidate
    if baseline is None or _revision_score(candidate) > _revision_score(baseline):
        if baseline is not None and not _measurement_compatible(candidate, baseline):
            candidate["status"] = "rejected_incompatible"
            st["latest_attempted_revision"] = dict(candidate)
            st["best_revision"] = dict(baseline)
            _restore_active_candidate(st, baseline)
            st["promoted_revision"] = None
            st["promotion_baseline"] = None
            st["screen_best_revision"] = None
            st["non_improving_attempts"] += 1
            return "rejected", candidate
        candidate["status"] = "best"
        st["best_revision"] = dict(candidate)
        st["active_revision"] = dict(candidate)
        st["setupslack_ns"] = candidate.get("setupslack_ns")
        st["non_improving_attempts"] = 0
        st["promoted_revision"] = None
        st["promotion_baseline"] = None
        st["screen_best_revision"] = None
        return "accepted", candidate
    st["best_revision"] = dict(baseline)
    _restore_active_candidate(st, baseline)
    candidate["status"] = "rejected_regression"
    st["latest_attempted_revision"] = dict(candidate)
    st["promoted_revision"] = None
    st["promotion_baseline"] = None
    st["screen_best_revision"] = None
    st["non_improving_attempts"] += 1
    return "rejected", candidate


def _begin_chat_run(session_id: str, *, require_architecture: bool = False,
                    resume_contract: bool = False) -> dict:
    st = _get_state(session_id)
    # The run budget is pinned at run start from the session's effective
    # reasoning effort (effort cannot change mid-run: chats serialize).
    chat_budget = _chat_budget(session_id)
    st.update({"chat_token_start": st["tokens_used"],
               "chat_budget": chat_budget,
               "controller_run_id": f"run-{uuid.uuid4().hex}",
               "syntax_attempts": 0, "repair_attempts": 0,
               "timing_violations": 0, "timing_diagnosis": None,
               "timing_retry_history": [], "synthesis_invocations": 0,
               "timing_diagnostic_measurements": set(),
               "timing_admission_rejections": 0,
               "seen_tool_calls": {}, "run_stop_reason": None,
               "non_improving_attempts": 0,
               "screen_attempts": 0, "screen_passes": 0,
               "screen_best_revision": None,
               "promoted_revision": None, "promotion_baseline": None,
               "full_pnr_invocations": 0, "full_pnr_active_hash": None,
               "full_pnr_result": None,
               "tool_attempts": 0, "current_round": 0,
               "planned_hashes": set(),
               "next_prompt_estimate": None,
               "context_warning_emitted": False,
               "stale_approval_continuations": 0,
               "architecture_required": bool(require_architecture),
               "contract_gate_pending": bool(require_architecture and not resume_contract),
               "current_request": None,
               "empty_continuations": 0, "continuation_instruction": None,
               "continuation_kwargs": None})
    _run_snapshot_for(session_id, lifecycle="running")
    _save_project(session_id)
    if require_architecture and not resume_contract:
        # A new design/change request must create a fresh proposal; the prior
        # locked binding cannot silently authorize the new model turn.
        st["contract_run_revision"] = None
        st["contract_run_digest"] = None
    if resume_contract:
        # Approval has already captured the exact revision/digest. Consume the
        # one-shot request marker while retaining the run binding.
        st["pending_request"] = None
        contract_state = st.get("design_contract")
        if isinstance(contract_state, dict):
            try:
                locked = _contract_state_from(contract_state)
                locked.pending_request = False
                locked.paused_reason = None
                locked.digest = locked.recompute_digest()
                st["design_contract"] = locked.canonical()
                # Lifecycle fields participate in the contract digest; bind
                # the active run to the post-admission canonical state.
                st["contract_run_revision"] = locked.revision
                st["contract_run_digest"] = locked.digest
                # This is a continuation of an already approved request, not
                # a new architecture discussion. Keep the instruction
                # transient: build_model_context consumes it for this
                # provider turn and the response handler clears it.
                st["continuation_instruction"] = (
                    "APPROVAL ALREADY SUCCEEDED: the design contract is locked "
                    f"at revision {locked.revision}. Continue implementing the "
                    "approved design in this turn. Do not ask for readiness or "
                    "approval, and do not call define_architecture unless you "
                    "are proposing a material change to executable architecture "
                    "fields; material changes must pause for explicit approval."
                )
                _save_project(session_id)
            except ContractProtocolError:
                pass
    return st


def _chat_tokens_used(st: dict) -> int:
    return max(0, st["tokens_used"] - st.get("chat_token_start", 0))


def _stop_message(st: dict, reason: str) -> str:
    st["run_stop_reason"] = reason
    return reason


def _before_tool_call(session_id: str, name: str, args: dict | None = None) -> str | None:
    st = _get_state(session_id)
    args = args or {}
    # Architecture-first admission applies to real design work.  Raw EDA
    # adapters remain usable for standalone diagnostics, but a design request
    # cannot reach RTL lint, simulation, or synthesis without an approved
    # contract and a source-level consistency check.
    design_tools = {"write_and_check_syntax", "run_simulation"} | _SYNTHESIS_NAMES
    if name in design_tools and st.get("architecture_required", True):
        code = args.get("verilog_code")
        contract_state = st.get("design_contract")
        locked = False
        locked_revision = None
        locked_digest = None
        if isinstance(contract_state, dict):
            try:
                parsed_contract = _contract_state_from(contract_state)
                locked = parsed_contract.status in {"locked", "approved"}
                locked_revision = parsed_contract.revision
                locked_digest = parsed_contract.digest
            except ContractProtocolError:
                pass
        if not locked:
            return ("CONTRACT BLOCKED: no locked design contract authorizes RTL or EDA work. "
                    "Review the proposal and use Approve & build (or answer required decisions first).")
        if st.get("contract_run_revision") not in {None, locked_revision} or st.get("contract_run_digest") not in {None, locked_digest}:
            return ("CONTRACT STALE: this run is bound to a different contract revision/digest. "
                    "Restart from the current locked contract before generating RTL.")
        contract = _architecture_contract_for(st)
        if isinstance(code, str):
            if contract is None:
                return ("ARCHITECTURE BLOCKED: locked contract has no valid architecture binding.")
            contradictions = validate_rtl_against_contract(
                contract, code, top_module=args.get("top_module"), tool=name, tool_args=args)
            if contradictions:
                return ("ARCHITECTURE CONTRADICTION: RTL/tool intent conflicts with the "
                        f"locked contract ({'; '.join(contradictions)}). "
                        "Revise the contract before retrying.")
    if name not in _SYNTHESIS_NAMES:
        return None
    phase = _synthesis_flow(args, name)
    code = args.get("verilog_code")
    digest = _code_sha256(code) if isinstance(code, str) else None
    diagnostic_key = None
    contract = _architecture_contract_for(st)
    if contract is not None and isinstance(code, str):
        if args.get("clock_period_ns") is None and not args.get("constraints"):
            return ("TIMING CONTRACT BLOCKED: clock_period_ns or an explicit constraints "
                    "contract is required; use the approved architecture clock target "
                    f"({contract.clock_period_ns:g} ns / {contract.clock_target_mhz:g} MHz).")
    # A full-P&R request with a VALID promotion token is bound to that token's
    # exact candidate. The model sometimes regenerates the RTL instead of
    # re-referencing the promoted source; that is a different, unverified
    # candidate and must be rejected with the corrective instruction here —
    # the generic simulation-checkpoint message below cannot name the
    # promoted handle. Everything else (unknown tokens, consumed experiments)
    # keeps its existing dedicated message further down the admission path.
    token = args.get("promotion_token")
    if digest is not None and isinstance(token, str) and token:
        promoted = st.get("promoted_revision") or {}
        if (promoted.get("promotion_token") != token
                and digest not in (st.get("full_pnr_by_hash") or {})):
            # Plain tool result, NOT a stop message: the corrective
            # instruction must let the model re-screen in the SAME run —
            # a stop would terminate the loop before it can act.
            return (
                "FULL PNR TOKEN UNKNOWN: no pending promotion matches this token "
                "(it did not survive a backend restart, was already consumed, or "
                "never existed). Re-earn it: run the cheap screening synthesis "
                "(plain run_silicon_compiler at the approved contract clock "
                "period, no flow argument) on the simulation-passed source "
                "artifact handle from CURRENT ARTIFACTS; an accepted screen "
                "returns a fresh promotion_token. Then request flow=full-pnr "
                "with that token and the promoted source handle — never a "
                "regenerated RTL.")
        if (promoted.get("promotion_token") == token
                and digest != promoted.get("code_sha256")):
            # Same as above: corrective, non-terminal.
            return (
                f"FULL PNR SOURCE MISMATCH: the promotion token belongs to "
                f"checkpoint sha256 {str(promoted.get('code_sha256'))[:16]}, but the "
                f"supplied RTL hashes to {digest[:16]} and was never simulated. Do "
                "not regenerate the RTL: call run_silicon_compiler with "
                "flow=full-pnr, the same promotion_token, and the promoted source "
                f"artifact handle [artifact rtl:{str(promoted.get('code_sha256'))[:16]}] "
                "from CURRENT ARTIFACTS. Re-run the cheap screen if no promotion is pending.")
    # Synthesis is only admitted for an RTL hash that has a passing simulation
    # checkpoint. Calls without verilog_code remain compatible with the direct
    # policy probes; real model calls always carry the source.
    if digest is not None:
        checkpoint = st.get("revision_by_hash", {}).get(digest)
        if (not checkpoint or not checkpoint.get("functional_passed") or
                (st.get("architecture_contract_hash") and
                 checkpoint.get("architecture_contract_hash") != st.get("architecture_contract_hash"))):
            return _stop_message(
                st,
                "Synthesis not started: this exact RTL has no passing simulation "
                "checkpoint. Run run_simulation successfully before synthesis.")
    if st.get("retry_constraint") and st.get("timing_diagnosis"):
        diagnosis = st["timing_diagnosis"]
        evidence_hash = diagnosis.get("code_sha256")
        stale_evidence = bool(digest and evidence_hash and evidence_hash != digest)
        classification = (
            diagnosis.get("critical_path_classification")
            or classify_timing_path(diagnosis)
        )
        if stale_evidence:
            # Measure a newly simulation-verified candidate once without
            # pretending the previous RTL's path proves a repair strategy.
            # Both synthesis entry points share this candidate/evidence key.
            identity = {
                "candidate": digest,
                "evidence": evidence_hash,
                "contract": st.get("architecture_contract_hash"),
                "clock_period_ns": args.get("clock_period_ns"),
                "constraints": args.get("constraints"),
                "target": args.get("target", "skywater130_demo"),
            }
            diagnostic_key = hashlib.sha256(
                json.dumps(identity, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            used = st.setdefault("timing_diagnostic_measurements", set())
            if diagnostic_key in used:
                return _stop_message(
                    st,
                    "Timing retry blocked: diagnostic allowance already consumed for "
                    "this candidate/evidence identity. "
                    "[reason_code=diagnostic_allowance_exhausted] No synthesis was started.")
            # Private controller metadata is stripped before calling the EDA
            # adapter and retained only for result/audit classification.
            args["_timing_diagnostic"] = {
                "reason_code": "stale_evidence",
                "candidate_sha256": digest,
                "evidence_sha256": evidence_hash,
                "identity": diagnostic_key,
            }
            intent = None
            validation = {"ok": True}
        elif classification.get("category") == "unknown":
            return _stop_message(
                st,
                "Timing retry stopped: measured critical path remains unclassified; "
                "no evidence-backed repair strategy can be authorized. Preserve the "
                "best known result and inspect the referenced timing report. "
                "[reason_code=diagnosis_inconclusive] No synthesis was started.")
        else:
            intent = args.get("timing_intent")
            validation = validate_timing_retry_intent(
                intent, diagnosis,
                require_supported_strategy=True,
                architecture_contract=st.get("architecture_contract"),
            )
        if not validation["ok"]:
            missing = ", ".join(validation.get("missing") or [])
            category = (validation.get("classification") or {}).get("category", "unknown")
            allowed = ", ".join(validation.get("allowed_strategies") or [])
            reason = validation.get("strategy_reason") or "an explicit evidence-supported strategy is required"
            reason_code = validation.get("reason_code", "invalid_timing_intent")
            st["timing_admission_rejections"] = int(
                st.get("timing_admission_rejections", 0) or 0) + 1
            suffix = ""
            if st["timing_admission_rejections"] >= MAX_TIMING_ADMISSION_REJECTIONS:
                suffix = (" Rejection guard exhausted; terminate this repair loop "
                          "and preserve the best known result.")
                _stop_message(
                    st,
                    "Timing admission rejection guard exhausted. "
                    "[reason_code=timing_admission_rejections_exhausted]")
            return ("Timing retry blocked: " + reason + ". "
                    f"Category={category}; allowed strategies={allowed}; "
                    f"missing/invalid={missing}. [reason_code={reason_code}]"
                    + suffix + " No synthesis was started.")
        strategy = (str(intent.get("strategy", "")).strip().lower()
                    if isinstance(intent, dict) else "")
        if (not stale_evidence and phase == "screen" and
                strategy in set(st.get("timing_retry_history") or [])):
            return ("Timing retry blocked: strategy " + strategy +
                    " was already measured for this run and may not be repeated. "
                    "Select a different strategy from the classified critical path. "
                    "No synthesis was started.")
        if phase == "screen" and not stale_evidence:
            st.setdefault("timing_retry_history", []).append(strategy)
    # Non-improvement bounds exploratory screens only. An already promoted
    # candidate must still be allowed to consume the separate full-P&R slot.
    if (phase == "screen" and
            st.get("non_improving_attempts", 0) >= MAX_NON_IMPROVING_ATTEMPTS):
        return _stop_message(
            st,
            f"Run stopped: no improving verified revision after "
            f"{MAX_NON_IMPROVING_ATTEMPTS} synthesis attempts. Restore the best "
            "checkpoint identified in the preceding tool result and change strategy "
            "before requesting another synthesis.")

    if phase == "screen":
        if st.get("screen_attempts", 0) >= MAX_SCREEN_INVOCATIONS:
            promoted = st.get("promoted_revision") or {}
            winner = promoted or st.get("screen_best_revision") or {}
            suffix = (
                f" Candidate r{winner.get('revision', '?')} remains promoted "
                f"(sha256 {str(winner.get('code_sha256', ''))[:12]}); "
                "the authorized full P&R experiment is still available."
                if winner else
                " No promoted candidate remains; keep the last measured checkpoint.")
            return _stop_message(
                st,
                f"Run stopped: cheap synthesis/STA screen limit reached "
                f"({MAX_SCREEN_INVOCATIONS}). No further cheap screens were started."
                + suffix)
        # Count admitted calls, including tool failures, exactly once. This is
        # intentionally separate from full_pnr_invocations.
        st["screen_attempts"] += 1

    if phase == "full_pnr":
        promoted = st.get("promoted_revision") or {}
        prior = st.get("full_pnr_by_hash", {}).get(digest) if digest else None
        checkpoint = st.get("revision_by_hash", {}).get(digest) if digest else None
        binding = _promotion_binding(args, checkpoint) if checkpoint else None
        token = args.get("promotion_token")
        if not isinstance(token, str) or not token.strip():
            return ("FULL P&R BLOCKED: promotion_token is required for every full "
                    "P&R request; use the token returned by the accepted cheap screen.")
        if prior is not None:
            return ("FULL P&R BLOCKED: this promoted candidate already consumed "
                    "its full P&R experiment. Do not repeat the expensive run.")
        if token != promoted.get("promotion_token"):
            return "FULL P&R BLOCKED: promotion token does not match the screened candidate/context."
        if (digest is None or promoted.get("code_sha256") != digest or
                promoted.get("promotion_binding") != binding):
            return ("FULL P&R BLOCKED: this exact RTL/context was not promoted by a "
                    "passing cheap synthesis/STA screen. Re-screen before full P&R.")
        if st.get("full_pnr_active_hash") == digest:
            return ("FULL P&R BLOCKED: an implementation experiment for this "
                    "promoted candidate is already admitted.")
        if st.get("full_pnr_invocations", 0) >= MAX_FULL_PNR_INVOCATIONS:
            return _stop_message(
                st,
                f"Run stopped: full P&R experiment limit reached "
                f"({MAX_FULL_PNR_INVOCATIONS}). Keep the best measured checkpoint "
                "or change the candidate through a new cheap screen.")
        st["full_pnr_invocations"] += 1
        st["full_pnr_active_hash"] = digest
    # This is telemetry for the total number of EDA calls, not a shared
    # admission cap. Screen and full-P&R budgets are enforced independently.
    if diagnostic_key is not None:
        st.setdefault("timing_diagnostic_measurements", set()).add(diagnostic_key)
    st["synthesis_invocations"] += 1
    return None


def _postprocess_tool_result(session_id: str, name: str, args: dict, result: str) -> str:
    """Enforce the repair budgets and append token usage to a tool result.

    Budgets (per session, in memory): SYNTAX_REPAIR_CAP failed syntax checks,
    TOTAL_REPAIR_CAP failed tool calls overall. On exhaustion the appended
    notice tells the agent to change strategy and include the diff of its
    attempted changes in its next reply; the last sim-passing snapshot is
    pointed out so it can be restored (the agent has that code in history).
    """
    st = _get_state(session_id)
    notices: list[str] = []
    # Keep direct/tool-loop proposal processing safe even when a caller sets
    # current_request without going through the async chat admission wrapper.
    if _is_frozen_matmul3_request(st.get("current_request")):
        st["benchmark_contract_id"] = "matmul3"

    if name == "define_architecture":
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            payload = None
        frozen_errors = (
            _frozen_matmul3_admission_errors(
                payload,
                st.get("current_request"),
                benchmark_contract_id=_benchmark_contract_id_for(st),
            )
            if isinstance(payload, dict) and payload.get("ok") and payload.get("approved")
            else []
        )
        if frozen_errors:
            # The stateless validator can only validate generic architecture
            # shape. Before this proposal reaches session/UI state, apply the
            # request-scoped immutable benchmark admission rule.
            payload = {
                "ok": False,
                "approved": False,
                "error": "frozen matmul3 contract mismatch: " + "; ".join(frozen_errors),
            }
            result = json.dumps(payload, sort_keys=True)
        if isinstance(payload, dict) and payload.get("ok") and payload.get("approved"):
            contract = payload.get("contract")
            digest = payload.get("contract_sha256")
            try:
                parsed = ArchitectureContract.model_validate(contract)
                expected = _architecture_contract_hash(parsed)
            except Exception:
                parsed = None
                expected = None
            current_hash = st.get("architecture_contract_hash")
            # ``rationale`` is explanatory text, not an executable
            # architecture constraint. During the one-shot approval resume a
            # model may repeat the approved fields while regenerating that
            # prose, so compare the locked executable fields as well as the
            # content hash. Otherwise an equivalent repeat is mistaken for a
            # material revision and the resumed run is paused for approval a
            # second time.
            locked_contract = None
            design_contract = st.get("design_contract")
            if isinstance(design_contract, dict):
                try:
                    locked_contract = _contract_state_from(design_contract)
                except ContractProtocolError:
                    locked_contract = None
            executable_repeat = False
            if parsed is not None and locked_contract is not None and locked_contract.status == "locked":
                prior_arch = st.get("architecture_contract")
                try:
                    prior_arch = ArchitectureContract.model_validate(prior_arch) if isinstance(prior_arch, dict) else None
                except Exception:
                    prior_arch = None
                if prior_arch is not None:
                    current_fields = parsed.canonical().copy()
                    prior_fields = prior_arch.canonical().copy()
                    current_fields.pop("rationale", None)
                    prior_fields.pop("rationale", None)
                    executable_repeat = current_fields == prior_fields
            # define_architecture validates a proposal, but it must never
            # silently authorize an external behavioral change. Internal
            # implementation choices remain reviewable in the transcript, but
            # do not reopen the user approval gate or discard measured evidence.
            internal_only_change = False
            if parsed is not None and locked_contract is not None and locked_contract.status == "locked":
                prior_arch = st.get("architecture_contract")
                try:
                    prior_arch = ArchitectureContract.model_validate(prior_arch) if isinstance(prior_arch, dict) else None
                except Exception:
                    prior_arch = None
                if prior_arch is not None:
                    previous_fields = prior_arch.canonical()
                    proposed_fields = parsed.canonical()
                    internal_fields = {"pipeline_stages", "multiplier_strategy",
                                       "memory_strategy", "rationale"}
                    all_fields = set(previous_fields) | set(proposed_fields)
                    internal_only_change = (
                        any(previous_fields.get(field) != proposed_fields.get(field)
                            for field in internal_fields)
                        and all(previous_fields.get(field) == proposed_fields.get(field)
                                for field in all_fields - internal_fields)
                    )
            if (parsed is not None and digest == expected and digest != current_hash
                    and internal_only_change):
                st["architecture_contract"] = parsed.canonical()
                st["architecture_contract_hash"] = digest
                # Keep the locked GenUI surface synchronized with the
                # executable internal strategy, while preserving its locked
                # lifecycle and without creating a new approval pause.
                if isinstance(design_contract, dict):
                    try:
                        locked = _contract_state_from(design_contract)
                        previous_surface = locked.canonical()
                        spec = locked.spec.model_copy(deep=True)
                        metadata = dict(spec.metadata or {})
                        metadata["architecture_contract"] = parsed.canonical()
                        spec.metadata = metadata
                        timing_goals = dict(spec.timing_goals or {})
                        timing_goals["pipeline_stages"] = parsed.pipeline_stages
                        spec.timing_goals = timing_goals
                        locked.spec = spec
                        locked.revision += 1
                        locked.pending_request = False
                        locked.paused_reason = None
                        locked.digest = locked.recompute_digest()
                        st["design_contract"] = locked.canonical()
                        st["contract_revision_diff"] = _contract_revision_diff(
                            previous_surface, st["design_contract"])
                        st["contract_run_revision"] = locked.revision
                        st["contract_run_digest"] = locked.digest
                    except ContractProtocolError:
                        logger.warning("[%s] could not synchronize internal architecture update to GenUI state", session_id)
                display_payload = dict(payload)
                display_payload.pop("approved", None)
                display_payload.update({"validated": True, "approval_required": False,
                                        "internal_optimization": True})
                result = json.dumps(display_payload, sort_keys=True)
                notices.append(
                    "ARCHITECTURE ALREADY APPROVED: internal optimization accepted; only "
                    "pipeline/DSP/memory strategy fields changed; the locked external contract remains approved. "
                    "Retain the last-good RTL checkpoint and measured baseline while "
                    "re-verifying the optimized implementation.")
            elif parsed is not None and digest == expected and digest != current_hash and not executable_repeat:
                st["architecture_contract"] = parsed.canonical()
                st["architecture_contract_hash"] = digest
                # A changed architecture invalidates every old checkpoint and
                # measured baseline; stale RTL must be re-simulated.
                st["revisions"] = []
                st["revision_by_hash"] = {}
                st["best_revision"] = None
                st["active_revision"] = None
                st["last_good_verilog"] = None
                st["screen_best_revision"] = None
                st["promoted_revision"] = None
                st["promotion_baseline"] = None
                st["non_improving_attempts"] = 0
                st["timing_violations"] = 0
                st["timing_diagnosis"] = None
                surface = _extract_design_contract(
                    _design_spec_from_architecture(parsed),
                    project_id=session_id,
                    session_id=session_id,
                )
                proposed = _contract_state_from(surface["contract"])
                previous = st.get("design_contract")
                if isinstance(previous, dict):
                    try:
                        prior_contract = _contract_state_from(previous)
                        proposed.revision = prior_contract.revision + 1
                    except ContractProtocolError:
                        pass
                proposed.digest = proposed.recompute_digest()
                if isinstance(previous, dict):
                    st["contract_revision_diff"] = _contract_revision_diff(previous, proposed.canonical())
                else:
                    st["contract_revision_diff"] = None
                st["design_contract"] = proposed.canonical()
                # A new architecture is never executable merely because the
                # stateless validator returned approved=true. It must become
                # the visible proposal and pause the tool loop.
                st["contract_gate_pending"] = True
                st["contract_run_revision"] = None
                st["contract_run_digest"] = None
                if not st.get("pending_request"):
                    request = st.get("current_request")
                    if isinstance(request, str) and request.strip():
                        st["pending_request"] = request
                display_payload = dict(payload)
                display_payload.pop("approved", None)
                display_payload["validated"] = True
                display_payload["approval_required"] = True
                result = json.dumps(display_payload, sort_keys=True)
                notices.append(
                    f"ARCHITECTURE PROPOSAL READY: {parsed.target_type}/{parsed.top_module}/{parsed.technology}, "
                    f"{parsed.clock_target_mhz:g} MHz, latency {parsed.latency_cycles} cycles, "
                    f"pipeline {parsed.pipeline_stages}; contract_sha256 {digest}.")
            elif parsed is not None and digest == expected and (digest == current_hash or executable_repeat):
                display_payload = dict(payload)
                display_payload.pop("approved", None)
                display_payload.update({"validated": True, "approval_required": False, "redundant": True})
                result = json.dumps(display_payload, sort_keys=True)
                notices.append(
                    "ARCHITECTURE ALREADY APPROVED: this proposal matches the locked "
                    "executable contract; continue directly with RTL generation and "
                    "verification. No new revision or approval is required.")
            else:
                notices.append("ARCHITECTURE REJECTED: validator output hash was invalid.")
        else:
            notices.append("ARCHITECTURE REJECTED: contract validation failed; revise the explicit fields and retry.")
    elif name == "write_and_check_syntax":
        code = args.get("verilog_code", "")
        if isinstance(code, str) and code and not _looks_like_artifact_handle(code):
            try:
                digest = _persist_candidate_source(
                    code, name=f"{args.get('top_module') or 'top'}.sv",
                    kind="rtl_candidate")
                notices.append(f"rtl_artifact_sha256: {digest}")
            except (EvidenceError, OSError) as exc:
                logger.error("lint candidate persistence failed: %s", exc,
                             extra={"session": session_id})
                st["run_stop_reason"] = "artifact_persistence_failed"
                notices.append(
                    "ARTIFACT PERSISTENCE FAILED: lint completed, but the exact RTL "
                    "could not be stored and read back by content hash. Stop before "
                    "simulation or synthesis.")
        if _lint_failed(result):
            st["syntax_attempts"] += 1
            st["repair_attempts"] += 1
            if st["syntax_attempts"] >= SYNTAX_REPAIR_CAP:
                notices.append(
                    f"SYNTAX REPAIR BUDGET EXHAUSTED: {st['syntax_attempts']} failed "
                    f"syntax checks this session (cap {SYNTAX_REPAIR_CAP}). Stop "
                    "retrying the same approach — change strategy (e.g. simplify the "
                    "construct, write a minimal module first). In your next reply, "
                    "include the diff of your attempted changes and explain the new plan:"
                    f"\n{_code_diff(st['prev_lint_code'], code)}")
        st["prev_lint_code"] = code
    elif name == "run_simulation":
        if "SIMULATION PASSED" in result:
            checkpoint_saved = _simulation_revision(session_id, args, result)
            if not checkpoint_saved:
                notices.append(
                    "CHECKPOINT REJECTED: simulation passed, but the exact RTL/testbench "
                    "could not be stored and read back by content hash. This revision is "
                    "not verified and synthesis is blocked.")
            contract_digest = st.get("architecture_contract_hash")
            if contract_digest and "architecture_contract_sha256:" not in result:
                notices.append(f"architecture_contract_sha256: {contract_digest}")
            st["syntax_attempts"] = 0  # real progress resets the syntax budget
        elif any(marker in result for marker in _SIM_FAILURES):
            st["repair_attempts"] += 1
            st["last_simulation_failure"] = str(result)
            good = st.get("last_good_verilog")
            good_hash = _code_sha256(good)[:16] if isinstance(good, str) and good else None
            if good_hash:
                notices.append(
                    "SIMULATION CANDIDATE FAILED: retained the last passing RTL "
                    f"checkpoint sha256={good_hash}; restore it before the next "
                    "optimization attempt and repair from that baseline.")
    elif (name in _SYNTHESIS_NAMES and
          not result.startswith(("Timing retry blocked:", "Timing retry stopped:",
                                 "FULL P&R BLOCKED:",
                                 "Run stopped:", "Synthesis not started:",
                                 "SAVED SOURCE REFERENCE BLOCKED:",
                                 "ARCHITECTURE BLOCKED:",
                                 "ARCHITECTURE CONTRADICTION:", "TIMING CONTRACT BLOCKED:"))):
        diagnostic = args.get("_timing_diagnostic")
        if isinstance(diagnostic, dict):
            notices.append(
                "TIMING DIAGNOSTIC MEASUREMENT: measured this exact verified "
                "candidate because the prior critical-path evidence belonged to "
                "different RTL. [reason_code=stale_evidence] "
                f"diagnostic_identity={diagnostic.get('identity', '')[:16]}")
        decision, snapshot = _synthesis_revision(session_id, args, result, name)
        if _is_full_pnr(args, name):
            digest = _code_sha256(args.get("verilog_code", ""))
            st["full_pnr_by_hash"][digest] = {"result": result, "decision": decision}
            st["full_pnr_result"] = result
            st["full_pnr_active_hash"] = None
        if decision in ("accepted", "promoted"):
            metrics = snapshot.get("metrics") or {}
            slack_text = metrics.get("setupslack")
            summary = f", setup slack {slack_text:g} ns" if isinstance(slack_text, (int, float)) else ""
            if decision == "promoted":
                notices.append(
                    f"REVISION ACCEPTED / CANDIDATE PROMOTED: cheap screen selected verified checkpoint "
                    f"r{snapshot['revision']} for the one allowed full P&R experiment "
                    f"(sha256 {snapshot['code_sha256'][:12]}{summary}; "
                    f"promotion_token {snapshot.get('promotion_token', '')}). To run it, call "
                    "run_silicon_compiler with flow=full-pnr, this exact promotion_token, and the "
                    f"promoted source artifact handle [artifact rtl:{snapshot['code_sha256'][:16]}] "
                    "from CURRENT ARTIFACTS. Never regenerate the RTL for the P&R request: a "
                    "rewritten candidate is unverified and will be rejected.")
            else:
                notices.append(
                    f"REVISION ACCEPTED: verified checkpoint r{snapshot['revision']} is now "
                    f"the best measured revision (sha256 {snapshot['code_sha256'][:12]}{summary}).")
        elif decision == "rejected":
            staged_screen = _synthesis_flow(args, name) == "screen" and bool(
                args.get("flow") or args.get("_controller_flow") or args.get("full_pnr"))
            best = (st.get("screen_best_revision") if staged_screen else None)
            best = best or st.get("best_revision") or {}
            st["repair_attempts"] += 1
            label = "best screened candidate" if staged_screen else "best revision"
            notices.append(
                f"REVISION REJECTED: synthesis did not improve the {label} "
                f"({st['non_improving_attempts']} of {MAX_NON_IMPROVING_ATTEMPTS} non-improving "
                f"attempts). Restore revision r{best.get('revision', '?')} "
                f"(sha256 {str(best.get('code_sha256', ''))[:12]}) from the prior simulation "
                "checkpoint before trying a different measured optimization.")
            if (st["non_improving_attempts"] >= MAX_NON_IMPROVING_ATTEMPTS and
                    not st.get("run_stop_reason") and
                    not staged_screen):
                _stop_message(
                    st,
                    f"Run stopped: no improving verified revision after "
                    f"{MAX_NON_IMPROVING_ATTEMPTS} synthesis attempts (non-improving cap). The best checkpoint "
                    f"is revision r{best.get('revision', '?')}.")
        elif decision == "unverified":
            st["repair_attempts"] += 1
            notices.append(
                "REVISION NOT CHECKPOINTED: synthesis was not preceded by a passing "
                "simulation for this exact RTL. Re-run simulation before optimizing it.")
        elif decision == "failed":
            st["repair_attempts"] += 1
            reason_code = ("infrastructure_failure"
                           if _timing_infrastructure_failure(result)
                           else "measurement_failed")
            notices.append("REVISION FAILED: synthesis evidence was not usable; the prior "
                           "verified checkpoint remains the baseline. "
                           f"[reason_code={reason_code}]")
        if result.startswith("Synthesis failed") or "find_fmax failed" in result:
            st["repair_attempts"] += 1
        slack = _synthesis_timing_violation(result)
        measured_metrics = _revision_metrics(result)
        contract = _architecture_contract_for(st)
        measured_fmax = _as_finite_float(measured_metrics.get("fmax_mhz"))
        target_mhz = contract.clock_target_mhz if contract is not None else None
        target_miss = (target_mhz is not None and measured_fmax is not None and
                       measured_fmax + 1e-6 < target_mhz)
        if slack is not None or target_miss:
            st["timing_violations"] += 1
            if slack is not None:
                timing_reason = f"measured setup slack {slack:g} ns"
            else:
                timing_reason = (f"measured Fmax {measured_fmax:g} MHz is below the "
                                 f"contract target {target_mhz:g} MHz")
            notices.append(
                f"TIMING VIOLATION: {timing_reason} "
                f"(violation {st['timing_violations']} of {MAX_TIMING_VIOLATIONS}).")
            classification = (st.get("timing_diagnosis") or {}).get("critical_path_classification")
            if classification:
                notices.append(
                    "TIMING CLASSIFICATION: "
                    f"{classification.get('category', 'unknown')} "
                    f"({classification.get('confidence', 'low')} confidence); "
                    "recommended strategies: " + ", ".join(
                        classification.get("recommended_strategies") or []
                    ))
            # Negative slack is evidence, not by itself a reason to stop: an
            # improved-but-still-negative revision can be useful. The revision
            # controller stops after bounded non-improvement. Keep the legacy
            # direct-call behavior only when no checkpoint exists, preserving
            # callers that use this private helper as a timing-policy probe.
            if (not st.get("revisions") and
                    st["timing_violations"] >= MAX_TIMING_VIOLATIONS):
                _stop_message(
                    st,
                    f"Run stopped: timing-closure limit reached "
                    f"({MAX_TIMING_VIOLATIONS} negative-slack synthesis results). "
                    "The design was not closed at the requested timing target.")
        # The screen ceiling is independent from full P&R. If the last
        # screen produced (or retained) a promotion token, report the boundary
        # but keep the run alive so the model can spend the one P&R slot.
        if (_synthesis_flow(args, name) == "screen" and
                st.get("screen_attempts", 0) >= MAX_SCREEN_INVOCATIONS):
            promoted = st.get("promoted_revision") or st.get("screen_best_revision") or {}
            if promoted:
                notices.append(
                    f"CHEAP SCREEN LIMIT REACHED ({MAX_SCREEN_INVOCATIONS}). "
                    f"Candidate r{promoted.get('revision', '?')} remains promoted "
                    f"(sha256 {str(promoted.get('code_sha256', ''))[:12]}); "
                    "one full P&R experiment remains authorized.")
            elif not st.get("run_stop_reason"):
                _stop_message(
                    st,
                    f"Run stopped: cheap synthesis/STA screen limit reached "
                    f"({MAX_SCREEN_INVOCATIONS}). No further cheap screens were started.")
    elif name in _SYNTHESIS_NAMES and result.startswith((
            "Timing retry blocked:", "Timing retry stopped:",
            "FULL P&R BLOCKED:", "Run stopped:",
            "Synthesis not started:", "ARCHITECTURE BLOCKED:",
            "ARCHITECTURE CONTRADICTION:", "TIMING CONTRACT BLOCKED:",
            "Timing retry blocked:")):
        # Admission failures are not synthesis attempts and must not consume
        # the non-improvement budget; the model may retry with intent (or must
        # first produce a promoted candidate for full P&R).
        notices.append(result)

    if st["repair_attempts"] >= TOTAL_REPAIR_CAP:
        note = (f"TOTAL REPAIR BUDGET EXHAUSTED: {st['repair_attempts']} failed tool "
                f"attempts this session (cap {TOTAL_REPAIR_CAP}). Change strategy: "
                "stop iterating blindly, re-read the first failing output, and explain "
                "your new plan in the next reply.")
        if st["last_good_verilog"] is not None:
            note += (" A design that passed simulation exists earlier in this "
                     "conversation — consider restoring it and working forward "
                     "from there with a different approach.")
        notices.append(note)

    if st["tokens_remaining"] is not None:
        notices.append(f"tokens_remaining: {st['tokens_remaining']}")
    else:
        notices.append(f"tokens_used (this session): {st['tokens_used']}")
    if st.get("next_prompt_estimate") is not None:
        notices.append(
            f"next_prompt_estimate (not API usage): {st['next_prompt_estimate']}")

    return result + "\n\n" + "\n".join(notices) if notices else result


class _CompletionTimeout(asyncio.TimeoutError):
    """A provider stream exceeded a bounded first-token, idle, or total wait."""

    def __init__(self, phase: str, timeout: float, *, partial_text: str = "",
                 has_tool_calls: bool = False, reasoning_chars: int = 0):
        self.phase = phase
        self.timeout = timeout
        self.partial_text = partial_text
        self.has_tool_calls = has_tool_calls
        self.reasoning_chars = reasoning_chars
        super().__init__(f"model stream {phase} timeout after {timeout:g}s")


def _chunk_value(value, key: str, default=None):
    """Read an OpenAI object or an OpenAI-compatible dict uniformly."""
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _stream_tool_call_parts(delta) -> list[object]:
    calls = _chunk_value(delta, "tool_calls", None)
    if calls:
        return list(calls)
    # A few older OpenAI-compatible endpoints still stream one legacy
    # function_call object rather than the modern indexed tool_calls list.
    function_call = _chunk_value(delta, "function_call", None)
    return [function_call] if function_call is not None else []


def _typed_content_chunk_items(content) -> list[tuple[str, str]]:
    """Split Mistral-style typed content chunks into ("thinking"|"text", s) pairs.

    Mistral's reasoning models (for example ``zai-glm-5-3``) deviate from the
    OpenAI wire format: the streamed answer arrives as a list of typed chunks
    inside ``delta.content`` instead of a plain string — a ThinkChunk
    (``{"type": "thinking", "thinking": [{"type": "text", "text": ...}]}``)
    followed by one or more TextChunks (``{"type": "text", "text": ...}``).
    No ``reasoning_content``/``reasoning`` field exists on the stream, so the
    thinking has to be unpacked from these chunks (docs.mistral.ai, "Handling
    thinking chunks"). Unrecognized items are skipped.
    """
    items: list[tuple[str, str]] = []
    if not isinstance(content, list):
        return items
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "thinking":
            inner = item.get("thinking")
            if isinstance(inner, str) and inner:
                items.append(("thinking", inner))
            elif isinstance(inner, list):
                for sub in inner:
                    if isinstance(sub, dict) and isinstance(sub.get("text"), str) and sub["text"]:
                        items.append(("thinking", sub["text"]))
        elif item_type == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                items.append(("text", text))
    return items


async def _await_stream_value(awaitable, *, deadline: float, round_no: int,
                             phase: str, timeout: float):
    """Yield heartbeat events while awaiting one provider operation.

    The final event is ``("value", result)``. Keeping this as an async
    generator lets the chat generator forward liveness frames without
    buffering the provider response.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                task.cancel()
                raise _CompletionTimeout(phase, timeout)
            heartbeat = max(0.001, min(float(STATUS_HEARTBEAT_S), remaining))
            try:
                value = await asyncio.wait_for(asyncio.shield(task), heartbeat)
            except StopAsyncIteration:
                yield ("end", None)
                return
            except asyncio.TimeoutError:
                if task.done():
                    # This is the provider operation's own exception, not the
                    # heartbeat wait expiring.
                    raise
                elapsed = int(time.monotonic() - (deadline - timeout))
                if phase == "first-token":
                    text = (f"Waiting for model response… {elapsed // 60}:"
                            f"{elapsed % 60:02d} elapsed (round {round_no})")
                else:
                    text = (f"Waiting for model stream… {elapsed // 60}:"
                            f"{elapsed % 60:02d} elapsed (round {round_no})")
                yield ("status", {"type": "status", "content": text})
            else:
                yield ("value", value)
                return
    finally:
        if task.done():
            # Retrieve exceptions so a cancelled/failed child is never left as
            # an unobserved task when a consumer closes the chat generator.
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass


async def _stream_completion(create_awaitable, *, round_no: int):
    """Normalize a streamed OpenAI completion into public/internal events.

    Public events are only ``text_delta`` and provider-explicit
    ``reasoning_delta``. Reasoning is never read from a final assistant object
    or guessed from ordinary content. Tool-call fragments are assembled by
    index and become a single interoperable message before the tool loop sees
    them. The final event is ``("complete", response, streamed)``.
    """
    started = time.monotonic()
    overall_limit = float(STREAM_OVERALL_TIMEOUT_S)
    overall_deadline = (started + overall_limit
                        if overall_limit > 0 else float("inf"))
    first_timeout = max(0.001, float(STREAM_FIRST_TOKEN_TIMEOUT_S))
    inactivity_timeout = max(0.001, float(STREAM_INACTIVITY_TIMEOUT_S))
    first_deadline = min(overall_deadline, started + first_timeout)
    create_task = asyncio.ensure_future(create_awaitable)
    stream = None
    iterator = None
    # Keep these outside the stream-start branch so a timeout while waiting
    # for the first chunk still carries a complete partial-state description.
    text_parts: list[str] = []
    reasoning_seen = 0
    tool_parts: dict[int, dict] = {}
    try:
        async for event, value in _await_stream_value(
                create_task, deadline=first_deadline, round_no=round_no,
                phase="first-token", timeout=first_timeout):
            if event == "status":
                yield event, value
            else:
                stream = value

        if not hasattr(stream, "__aiter__"):
            # Compatibility path for local fakes and older adapters. Real SDK
            # calls are made with stream=True below and return AsyncStream.
            yield ("complete", stream, False)
            return

        yield ("stream_started", None)
        iterator = stream.__aiter__()
        text_parts: list[str] = []
        tool_parts: dict[int, dict] = {}
        saw_output = False
        latest_usage = None
        latest_finish_reason = None
        last_chunk = time.monotonic()
        while True:
            now = time.monotonic()
            if not saw_output:
                deadline = min(overall_deadline, started + first_timeout)
                phase = "first-token"
                timeout = first_timeout
            else:
                deadline = min(overall_deadline, last_chunk + inactivity_timeout)
                phase = ("overall" if overall_deadline <= last_chunk + inactivity_timeout
                         else "inactivity")
                timeout = (STREAM_OVERALL_TIMEOUT_S if phase == "overall"
                           else inactivity_timeout)
            next_task = iterator.__anext__()
            try:
                async for event, value in _await_stream_value(
                        next_task, deadline=deadline, round_no=round_no,
                        phase=phase, timeout=timeout):
                    if event == "status":
                        yield event, value
                    elif event == "end":
                        break
                    else:
                        chunk = value
                else:
                    # The helper yields a value/end event and then returns;
                    # whether it was a clean end is carried by ``ended``.
                    pass
                if event == "end":
                    break
            except StopAsyncIteration:
                break
            last_chunk = time.monotonic()
            usage = _chunk_value(chunk, "usage", None)
            if usage is not None:
                latest_usage = usage
            choices = _chunk_value(chunk, "choices", None) or []
            if not choices:
                # Usage-only final chunks are expected when
                # stream_options.include_usage is enabled.
                continue
            for choice in choices:
                finish_reason = _chunk_value(choice, "finish_reason", None)
                if finish_reason:
                    latest_finish_reason = str(finish_reason)
                delta = _chunk_value(choice, "delta", None)
                if delta is None:
                    continue
                reasoning = None
                for key in ("reasoning_content", "reasoning"):
                    candidate = _chunk_value(delta, key, None)
                    if not candidate:
                        candidate = _chunk_value(choice, key, None)
                    if isinstance(candidate, str) and candidate:
                        reasoning = candidate
                        break
                if reasoning:
                    saw_output = True
                    reasoning_seen += len(reasoning)
                    # Reasoning is displayed in a folded, explicitly
                    # user-controlled tile. Preserve every provider-exposed
                    # delta for the lifetime of this run; silently dropping
                    # the oldest text makes long design generations
                    # impossible to audit and gives a false impression that
                    # the model omitted part of its work.
                    yield ("reasoning_delta", {"type": "reasoning_delta",
                                                "content": reasoning})
                content = _chunk_value(delta, "content", None)
                if isinstance(content, str) and content:
                    saw_output = True
                    text_parts.append(content)
                    yield ("text_delta", {"type": "text_delta",
                                           "content": content})
                elif content is not None:
                    # Mistral reasoning models stream typed chunks inside
                    # delta.content (see _typed_content_chunk_items). Without
                    # this branch every chunk is silently dropped, the
                    # watchdog never sees output, and the run dies on the
                    # first-token timeout while the model is thinking.
                    for kind, chunk_text in _typed_content_chunk_items(content):
                        saw_output = True
                        if kind == "thinking":
                            reasoning_seen += len(chunk_text)
                            yield ("reasoning_delta", {"type": "reasoning_delta",
                                                       "content": chunk_text})
                        else:
                            text_parts.append(chunk_text)
                            yield ("text_delta", {"type": "text_delta",
                                                  "content": chunk_text})
                for index, part in enumerate(_stream_tool_call_parts(delta)):
                    if part is None:
                        continue
                    idx = _chunk_value(part, "index", index)
                    try:
                        idx = int(idx)
                    except (TypeError, ValueError):
                        idx = index
                    target = tool_parts.setdefault(idx, {
                        "id": None, "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    call_id = _chunk_value(part, "id", None)
                    if call_id:
                        target["id"] = call_id
                        saw_output = True
                    call_type = _chunk_value(part, "type", None)
                    if call_type:
                        target["type"] = call_type
                    function = _chunk_value(part, "function", part)
                    name = _chunk_value(function, "name", None)
                    arguments = _chunk_value(function, "arguments", None)
                    if name:
                        saw_output = True
                        target["function"]["name"] += str(name)
                    if arguments:
                        saw_output = True
                        target["function"]["arguments"] += str(arguments)
        # Reconstruct the exact message shape consumed by the existing loop.
        tool_calls = [tool_parts[index] for index in sorted(tool_parts)]
        dumped = {"role": "assistant"}
        text = "".join(text_parts)
        if text:
            dumped["content"] = text
        if tool_calls:
            dumped["tool_calls"] = tool_calls
        message = SimpleNamespace(
            content=text or None,
            tool_calls=[SimpleNamespace(
                id=call.get("id"), type=call.get("type", "function"),
                function=SimpleNamespace(
                    name=call["function"].get("name", ""),
                    arguments=call["function"].get("arguments", "")))
                for call in tool_calls] or None,
            model_dump=lambda exclude_none=True, _dumped=dumped: _dumped,
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=message,
                                     finish_reason=latest_finish_reason)],
            usage=locals().get("latest_usage"),
        )
        yield ("complete", response, True)
    except _CompletionTimeout as exc:
        # Preserve stream state for the caller. In particular, a live stream
        # can have emitted useful assistant text before becoming idle; losing
        # that information made the timeout message falsely claim that no
        # partial response existed.
        exc.partial_text = "".join(text_parts)
        exc.has_tool_calls = bool(tool_parts)
        exc.reasoning_chars = reasoning_seen
        raise
    except asyncio.CancelledError:
        create_task.cancel()
        if iterator is not None and hasattr(iterator, "aclose"):
            try:
                await iterator.aclose()
            except Exception:
                pass
        raise
    finally:
        if not create_task.done():
            create_task.cancel()


async def chat(message: str, session_id: str = "default", *,
               resume_contract: bool = False) -> AsyncGenerator[dict, None]:
    """Yield {"type": "tool_call"|"tool_result"|"text", "content": ...} events."""
    last_error = None
    try:
        async for event in _chat_inner(message, session_id, resume_contract=resume_contract):
            if isinstance(event, dict) and event.get("type") == "error":
                last_error = str(event.get("content") or "Run stopped with an error.")
            yield event
    except asyncio.CancelledError:
        _run_snapshot_for(session_id, lifecycle="cancelled",
                          terminal_reason="Run cancelled by user.", increment=True)
        raise
    except Exception as exc:
        _run_snapshot_for(session_id, lifecycle="failed",
                          terminal_reason=f"Run failed: {exc}", increment=True)
        raise
    else:
        st = _get_state(session_id)
        if st.get("contract_gate_pending") or st.get("pending_request"):
            lifecycle = "awaiting approval"
            reason = "Awaiting contract approval."
        elif st.get("run_stop_reason"):
            reason = str(st["run_stop_reason"])
            lifecycle = "inconclusive" if "inconclusive" in reason.lower() or "diagnosis" in reason.lower() else "failed"
        elif last_error:
            lifecycle, reason = "failed", last_error
        else:
            lifecycle, reason = "completed", None
        _run_snapshot_for(session_id, lifecycle=lifecycle, terminal_reason=reason, increment=True)
    finally:
        _save_project(session_id)


async def _chat_inner(message: str, session_id: str, *,
                      resume_contract: bool = False) -> AsyncGenerator[dict, None]:
    try:
        client, model_id = _client_for(session_id)
    except RuntimeError as e:
        yield {"type": "error", "content": str(e)}
        return
    history = _get_history(session_id)
    effective_request = _effective_request(message, history)
    requires_architecture = _architecture_request_required(effective_request)
    # A page/backend restart loses the in-flight continuation flag, but the
    # approved contract is durable. Only a terse recovery command may reclaim
    # that one-shot continuation; explicit new requests remain gated.
    implicit_resume = False
    if not resume_contract:
        implicit_resume = _implicit_contract_resume(
            message, effective_request, _get_state(session_id))
    run_resume = resume_contract or implicit_resume
    st = _begin_chat_run(
        session_id, require_architecture=requires_architecture,
        resume_contract=run_resume)
    # Admission and benchmark context follow the substantive request even when
    # the visible user turn is only "retry" or "continue".
    st["current_request"] = effective_request
    if _is_frozen_matmul3_request(effective_request):
        st["benchmark_contract_id"] = "matmul3"
        _save_project(session_id)
    if run_resume:
        # Clear the persisted pending-request marker in every watching client
        # before generation continues from the locked revision.
        resumed_contract = get_design_contract(session_id)
        if resumed_contract:
            yield {"type": "contract_state", **resumed_contract}
    if requires_architecture and not run_resume:
        st["pending_request"] = message
        st["contract_gate_pending"] = True
        _save_project(session_id)
    with _project_lock(session_id):
        # A manual terse retry is a real user-visible turn and must survive a
        # refresh. Only the server-generated approve-and-build continuation
        # reuses the already persisted original request.
        if not resume_contract:
            history.append({"role": "user", "content": message})
    logger.info("[%s] user: %s", session_id, _truncate(message, 500))

    for round_no in range(1, MAX_TOOL_ROUNDS + 1):
        st["current_round"] = round_no
        round_snapshot = _run_snapshot_for(
            session_id, lifecycle="running",
            event={"content": {"phase": "generation", "status": "running",
                                "attempt": round_no,
                                "budgets": _event_budgets(st, round_no=round_no)}},
            increment=True)
        yield {"type": "run_state", **round_snapshot}
        if st["run_stop_reason"] is not None:
            stop = st["run_stop_reason"]
            with _project_lock(session_id):
                history.append({"role": "assistant", "content": stop})
            yield _stop_decision_event(stop)
            yield {"type": "text", "content": stop}
            return
        used = _chat_tokens_used(st)
        budget = int(st.get("chat_budget") or CHAT_TOKEN_BUDGET)
        if used >= budget:
            stop = _stop_message(
                st,
                f"Run stopped before model round {round_no}: chat token budget "
                f"of {budget:,} was reached ({used:,} used).")
            with _project_lock(session_id):
                history.append({"role": "assistant", "content": stop})
            logger.warning("[%s] %s", session_id, stop)
            yield _stop_decision_event(stop)
            yield {"type": "text", "content": stop}
            return
        generation_id = f"gen-{uuid.uuid4().hex[:16]}"
        model_context = build_model_context(history, st)
        preflight = _context_preflight(session_id, model_context, TOOLS)
        context_event = _structured_event(
            "progress", "context", "Prepared bounded model context",
            "blocked" if preflight["hard_exceeded"] else "done",
            evidence=(f"round {round_no}; {len(model_context)} messages; "
                      f"estimated prompt {preflight['estimate']:,} tokens; "
                      f"reserved output {preflight['output_allowance']:,}"),
            attempts=_event_attempts(st, round_no=round_no),
            budgets={**_event_budgets(st, round_no=round_no),
                     "context": {"estimate": preflight["estimate"],
                                 "soft_limit": preflight["soft_limit"],
                                 "hard_limit": preflight["hard_limit"],
                                 "output_allowance": preflight["output_allowance"],
                                 "envelope": preflight["envelope"],
                                 "hard_exceeded": preflight["hard_exceeded"]}},
        )
        if preflight["hard_exceeded"] or preflight["run_exceeded"]:
            # Keep the long-standing first-frame contract: status is emitted
            # before any new structured timeline event, even on admission stop.
            yield {"type": "status", "content": f"Context limit check… (round {round_no})"}
            yield context_event
            if preflight["run_exceeded"]:
                stop = _stop_message(
                    st, f"Run stopped before model round {round_no} and before tool "
                    f"execution: no model-token allowance remains in the "
                    f"{int(st.get('chat_budget') or CHAT_TOKEN_BUDGET):,}-token run budget.")
            else:
                stop = _stop_message(
                    st,
                    f"Run stopped before model round {round_no}: estimated input "
                    f"{preflight['estimate']:,} plus reserved output "
                    f"{preflight['output_allowance']:,} exceeds the "
                    f"hard context limit/model envelope of "
                    f"{preflight['hard_limit']:,} tokens. "
                    "Start a fresh run or reduce retained context.")
            with _project_lock(session_id):
                history.append({"role": "assistant", "content": stop})
            logger.warning("[%s] %s", session_id, stop)
            yield _stop_decision_event(stop)
            yield {"type": "text", "content": stop}
            return
        if preflight["soft_warning"] and not st.get("context_warning_emitted"):
            st["context_warning_emitted"] = True
            warning = (
                f"Context warning: estimated next prompt is "
                f"{preflight['estimate']:,} tokens (soft limit "
                f"{preflight['soft_limit']:,}); API usage remains "
                f"{st['tokens_used']:,} tokens for this session.")
            logger.warning("[%s] %s", session_id, warning)
            yield {"type": "status", "content": warning}
        logger.debug("[%s] LLM round %d, durable_history=%d model_context=%d chars/%d messages, next_prompt_estimate=%d",
                     session_id, round_no, len(history), _context_size(model_context),
                     len(model_context), preflight["estimate"])
        # Await the completion as a task so a heartbeat status event can be
        # emitted every STATUS_HEARTBEAT_S while the model generates (the
        # first event goes out immediately, covering the first interval too).
        # Existing clients/tests expect this immediate status frame first.
        # The structured context event follows it and is still replayable.
        yield {"type": "status",
               "content": f"Waiting for model response… (round {round_no})"}
        response = None
        streamed = False
        generation_started = False
        try:
            async for stream_event in _stream_completion(
                    client.chat.completions.create(
                        model=model_id,
                        messages=model_context,
                        tools=TOOLS,
                        max_tokens=preflight["output_allowance"],
                        stream=True,
                        stream_options={"include_usage": True},
                        # The low-effort retry knob overrides the per-model
                        # default effort while a continuation is pending.
                        **(st.get("continuation_kwargs")
                           or _model_effort_kwargs(session_id)),
                    ), round_no=round_no):
                kind = stream_event[0]
                if kind in {"status", "text_delta", "reasoning_delta"}:
                    yield stream_event[1]
                elif kind == "stream_started":
                    streamed = True
                    generation_started = True
                    yield {"type": "generation_start", "round": round_no,
                           "generation_id": generation_id}
                elif kind == "complete":
                    response = stream_event[1]
                    streamed = bool(stream_event[2])
                    if not generation_started:
                        generation_started = True
                        yield {"type": "generation_start", "round": round_no,
                               "generation_id": generation_id}
                    break
        except _CompletionTimeout as exc:
            # A complete tool call is required before it can enter durable
            # history. Plain assistant text is safe to preserve, however, and
            # gives the next retry useful continuation context. Provider
            # reasoning remains live-only and is already visible in the folded
            # console; it is intentionally not promoted into the model prompt.
            partial_text = str(getattr(exc, "partial_text", "") or "")
            partial_saved = bool(partial_text.strip()) and not bool(
                getattr(exc, "has_tool_calls", False))
            if partial_saved:
                partial_note = (
                    "\n\n[The response was interrupted before completion; "
                    "continue from this point on retry.]"
                )
                with _project_lock(session_id):
                    history.append({"role": "assistant",
                                    "content": partial_text + partial_note})
            if partial_saved:
                detail = (
                    " Partial assistant text was preserved in session history; "
                    "retry to continue from it.")
            elif getattr(exc, "reasoning_chars", 0):
                detail = (
                    " Partial reasoning remains visible in the folded console; "
                    "no incomplete tool action was committed. Retry to continue.")
            elif getattr(exc, "has_tool_calls", False):
                detail = (
                    " An incomplete tool call was discarded safely; retry to "
                    "continue from the preserved project state.")
            else:
                detail = " Retry to continue from the preserved project state."
            timeout_message = (
                f"Model response stopped: {exc.phase} timeout after "
                f"{exc.timeout:g}s.{detail}")
            logger.warning("[%s] %s", session_id, timeout_message)
            yield {"type": "generation_end", "round": round_no,
                   "generation_id": generation_id, "status": "timeout",
                   "phase": exc.phase, "partial_saved": partial_saved,
                   "reasoning_chars": getattr(exc, "reasoning_chars", 0),
                   "finish_reason": None}
            yield {"type": "error", "content": timeout_message}
            return
        except (asyncio.CancelledError, GeneratorExit):
            # _stream_completion owns the provider task and closes the stream.
            raise
        except Exception:
            logger.exception("[%s] LLM request failed (round %d)", session_id, round_no)
            raise
        # Do not insert structured progress between heartbeat frames. Once the
        # completion resolves, publish the compact-context update before
        # processing the response; established status/heartbeat ordering stays
        # intact for clients that use the second yield as liveness evidence.
        if response is None:
            raise RuntimeError("provider returned no completion")
        yield context_event
        choice = response.choices[0]
        msg = choice.message
        # A successful provider response consumes the one-shot recovery hint.
        st["continuation_instruction"] = None
        st["continuation_kwargs"] = None
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason is None and isinstance(choice, dict):
            finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            finish_reason = str(finish_reason)
        usage = getattr(response, "usage", None)
        if usage:
            _record_usage(session_id, usage,
                          generation_id=generation_id,
                          run_id=st.get("controller_run_id"))
            if isinstance(usage, dict):
                prompt_used = usage.get("prompt_tokens")
                completion_used = usage.get("completion_tokens")
            else:
                prompt_used = getattr(usage, "prompt_tokens", None)
                completion_used = getattr(usage, "completion_tokens", None)
            logger.info(
                "[%s] round %d tokens: prompt=%s completion=%s (next prompt estimate=%s)",
                session_id, round_no, prompt_used, completion_used,
                st.get("next_prompt_estimate"),
            )
            # The context event was emitted before the provider returned.
            # Publish a normalized usage frame before the structured budget
            # snapshot so the UI can update the compact generation tile.
            normalized = _normalized_usage(usage)
            yield {"type": "generation_usage", "round": round_no,
                   "generation_id": generation_id, **normalized}
            # Publish a second, fresh budget snapshot after accounting usage so
            # the UI never displays the previous round's token total.
            yield _structured_event(
                "progress", "usage", "Token usage updated", "done",
                evidence=(f"provider usage: prompt={prompt_used or 0}, "
                          f"completion={completion_used or 0}, "
                          f"session total={st.get('tokens_used', 0)}"),
                attempts=_event_attempts(st, round_no=round_no),
                budgets=_event_budgets(st, round_no=round_no),
            )
        msg = response.choices[0].message

        # Record only the interoperable assistant protocol fields. Providers
        # may include very large private reasoning traces in model_dump();
        # those must never enter durable history or the next prompt.
        with _project_lock(session_id):
            dumped = msg.model_dump(exclude_none=True)
            clean_message = _sanitize_message(dumped)
            if clean_message:
                history.append(clean_message)
        if streamed:
            # The UI has already received deltas. Persist/replay only the one
            # assembled message above; this terminal event prevents duplicate
            # rendering while marking the generation complete.
            yield {"type": "generation_end", "round": round_no,
                   "generation_id": generation_id, "status": "complete",
                   "has_tool_calls": bool(msg.tool_calls),
                   "finish_reason": finish_reason}

        # A response that crosses the budget is the last model response we
        # accept: never launch an EDA tool after the hard ceiling is reached.
        if _chat_tokens_used(st) >= int(st.get("chat_budget") or CHAT_TOKEN_BUDGET):
            budget = int(st.get("chat_budget") or CHAT_TOKEN_BUDGET)
            stop = _stop_message(
                st,
                f"Run stopped before tool execution: chat token budget of "
                f"{budget:,} was reached "
                f"({_chat_tokens_used(st):,} used).")
            with _project_lock(session_id):
                history.append({"role": "assistant", "content": stop})
            logger.warning("[%s] %s", session_id, stop)
            yield _stop_decision_event(stop)
            yield {"type": "text", "content": stop}
            return

        if msg.tool_calls:
            for call in msg.tool_calls:
                name = call.function.name
                try:
                    raw_args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    raw_args = {}
                if not isinstance(raw_args, dict):
                    raw_args = {}
                # Resolve compact source handles before canonicalization and
                # every admission/tool boundary. Keep the compact form for
                # public frames so a large RTL blob is not duplicated in the UI.
                args, artifact_error = _resolve_artifact_handles(session_id, raw_args)
                # The approved contract is authoritative for Fmax measurement.
                # Fill only an omitted period; an explicit model value remains
                # visible and is checked for mismatch by admission.
                if (name == "find_fmax" and artifact_error is None and
                        args.get("clock_period_ns") is None and
                        not args.get("constraints")):
                    approved = _architecture_contract_for(st)
                    if approved is not None:
                        args = dict(args)
                        args["clock_period_ns"] = approved.clock_period_ns
                display_args = _compact_tool_call_args(
                    args if artifact_error is None else raw_args)
                canonical = _canonical_tool_call(name, args)
                logger.info("[%s] tool_call %s args=%s", session_id, name,
                            _truncate(json.dumps(display_args)))
                rtl_code = args.get("verilog_code")
                rtl_hash = (_code_sha256(rtl_code)
                            if artifact_error is None and
                            isinstance(rtl_code, str) and rtl_code.strip()
                            else None)
                if rtl_hash and rtl_hash not in st["planned_hashes"]:
                    st["planned_hashes"].add(rtl_hash)
                    yield _structured_event(
                        "progress", "revision",
                        f"Planning revision {len(st['planned_hashes'])}", "running",
                        attempts=_event_attempts(st, round_no=round_no),
                        budgets=_event_budgets(st, round_no=round_no),
                    )
                yield {"type": "tool_call",
                       "content": json.dumps({"name": name, "args": display_args})}

                previous = st["seen_tool_calls"].get(canonical)
                if previous is not None and not previous["failed"]:
                    result = ("DUPLICATE TOOL CALL SUPPRESSED: this exact call already "
                              "returned a non-failing result; no duplicate job was "
                              "started.")
                else:
                    st["tool_attempts"] = int(st.get("tool_attempts", 0) or 0) + 1
                    yield _tool_progress_event(name, "", started=True, args=display_args,
                                               state=st, round_no=round_no)
                    if artifact_error is not None:
                        result = "SAVED SOURCE REFERENCE BLOCKED: " + artifact_error
                    else:
                        blocked = _before_tool_call(session_id, name, args)
                        if blocked is not None:
                            result = blocked
                        else:
                            func = _TOOL_FUNCS.get(name)
                            if func is None:
                                result = f"Unknown tool: {name}"
                            else:
                                try:
                                    # Tag jobs submitted by this tool call with the
                                    # owning session (eda_tools reads the contextvar).
                                    _tool_token = _tool_session.set(session_id)
                                    try:
                                        eda_args = dict(args)
                                        eda_args.pop("timing_intent", None)
                                        eda_args.pop("_timing_diagnostic", None)
                                        # Admission-only credential: the controller
                                        # validates promotion_token in _before_tool_call;
                                        # never pass it to the EDA adapter.
                                        eda_args.pop("promotion_token", None)
                                        # Phase 3 defaults an unspecified SC run to
                                        # the cheap synthesis/STA screen when the
                                        # adapter supports an explicit flow keyword.
                                        # Older adapters remain backward compatible
                                        # (their signature simply lacks ``flow``).
                                        if (name == "run_silicon_compiler" and
                                                "flow" not in eda_args):
                                            try:
                                                accepts_flow = (
                                                    "flow" in inspect.signature(func).parameters)
                                            except (TypeError, ValueError):
                                                accepts_flow = False
                                            if accepts_flow:
                                                eda_args["flow"] = "syn-sta"
                                        result = func(**eda_args)
                                        if inspect.isawaitable(result):
                                            result = await result
                                    finally:
                                        _tool_session.reset(_tool_token)
                                except TypeError as e:
                                    result = f"Bad tool arguments: {e}"
                                except Exception as e:
                                    logger.exception("[%s] tool %s raised", session_id, name)
                                    result = f"Tool error: {e}"
                    policy_args = dict(args)
                    if (name == "run_silicon_compiler" and
                            "flow" not in policy_args):
                        # The controller's default is staged even when the
                        # legacy model call omitted a flow selector. Keep this
                        # private marker out of the provider/tool arguments.
                        policy_args["_controller_flow"] = "syn-sta"
                    result = _postprocess_tool_result(
                        session_id, name, policy_args, str(result))
                    st["seen_tool_calls"][canonical] = {
                        "result": str(result),
                        "failed": _recognized_tool_failure(name, str(result)),
                    }

                logger.info("[%s] tool_result %s: %s", session_id, name,
                            _truncate(str(result)))
                yield {"type": "tool_result", "content": str(result)}
                if name == "define_architecture":
                    contract_state = get_design_contract(session_id)
                    if contract_state:
                        yield {"type": "contract_state", **contract_state}
                    # A normal design prompt stops after producing the proposal.
                    # Approval resumes this exact request through the controller.
                    if st.get("contract_gate_pending") and contract_state:
                        parsed = _contract_state_from(contract_state["contract"])
                        parsed.pending_request = True
                        parsed.paused_reason = (
                            "answer_required_decisions" if parsed.status == "needs_decision"
                            else "awaiting_contract_approval")
                        parsed.digest = parsed.recompute_digest()
                        st["design_contract"] = parsed.canonical()
                        _save_project(session_id)
                        paused = get_design_contract(session_id)
                        if paused:
                            yield {"type": "contract_state", **paused}
                        pause_text = (
                            "Design contract proposal ready. Answer the required decisions, "
                            "then use the Approve & Build button to start the build." if parsed.status == "needs_decision"
                            else "Design contract proposal ready. Use the Approve & Build button to start the build.")
                        with _project_lock(session_id):
                            history.append({"role": "tool", "tool_call_id": call.id,
                                            "content": str(result)})
                            history.append({"role": "assistant", "content": pause_text})
                        yield {"type": "text", "content": pause_text}
                        return
                yield _tool_progress_event(name, str(result), args=display_args,
                                           state=st, round_no=round_no)
                if name in _SYNTHESIS_NAMES:
                    failed_slack = _synthesis_timing_violation(str(result))
                    timing_metrics = _revision_metrics(str(result))
                    contract = _architecture_contract_for(st)
                    measured_fmax = _as_finite_float(timing_metrics.get("fmax_mhz"))
                    target_mhz = contract.clock_target_mhz if contract is not None else None
                    target_miss = (target_mhz is not None and measured_fmax is not None and
                                   measured_fmax + 1e-6 < target_mhz)
                    if failed_slack is not None:
                        yield _structured_event(
                            "progress", "timing",
                            f"Timing failed: −{abs(failed_slack):g} ns", "failed",
                            reason="Measured setup slack is below zero.",
                            evidence=f"Measured setup slack: {failed_slack:g} ns",
                            setupslack_ns=failed_slack,
                            metrics={"setupslack_ns": failed_slack},
                            attempts=_event_attempts(st, round_no=round_no),
                            budgets=_event_budgets(st, round_no=round_no),
                        )
                    elif target_miss:
                        yield _structured_event(
                            "progress", "timing",
                            f"Timing below target: {measured_fmax:g} MHz", "failed",
                            reason=f"Measured Fmax is below the {target_mhz:g} MHz architecture contract.",
                            evidence=f"Measured Fmax: {measured_fmax:g} MHz; target: {target_mhz:g} MHz",
                            fmax_mhz=measured_fmax, target_mhz=target_mhz,
                            attempts=_event_attempts(st, round_no=round_no),
                            budgets=_event_budgets(st, round_no=round_no),
                        )
                    classification = (st.get("timing_diagnosis") or {}).get("critical_path_classification")
                    if classification and (failed_slack is not None or target_miss):
                        yield _structured_event(
                            "decision", "timing",
                            "Critical path classified", "done",
                            category=classification.get("category"),
                            confidence=classification.get("confidence"),
                            evidence=classification.get("evidence"),
                            recommended_strategies=classification.get("recommended_strategies"),
                            attempts=_event_attempts(st, round_no=round_no),
                            budgets=_event_budgets(st, round_no=round_no),
                        )
                decision_event = _decision_event_for_result(
                    name, str(result), state=st, round_no=round_no)
                if decision_event is not None:
                    yield decision_event
                    if (decision_event["content"].get("status") in {"rejected", "failed"}
                            and st.get("best_revision")):
                        best = st["best_revision"]
                        yield _structured_event(
                            "decision", "revision",
                            f"Restored revision {best.get('revision', '?')}", "done",
                            reason="Rejected or failed candidate cannot replace the measured baseline.",
                            attempts=_event_attempts(st, round_no=round_no),
                            budgets=_event_budgets(st, round_no=round_no),
                        )
                with _project_lock(session_id):
                    history.append({"role": "tool", "tool_call_id": call.id,
                                    "content": str(result)})
                if st["run_stop_reason"] is not None:
                    stop = st["run_stop_reason"]
                    with _project_lock(session_id):
                        history.append({"role": "assistant", "content": stop})
                    yield _stop_decision_event(stop)
                    yield {"type": "text", "content": stop}
                    return
            continue

        text = msg.content or ""
        if text:
            if (_stale_approval_request(text, st) and
                    int(st.get("stale_approval_continuations", 0) or 0) < 1):
                st["stale_approval_continuations"] = 1
                correction = (
                    "No approval is pending: the behavioral contract is already locked. "
                    "The internal timing optimization is continuing automatically."
                )
                # Replace the just-persisted misleading terminal prose so a
                # refresh cannot resurrect it. Streamed deltas may already be
                # visible in the current browser, so also emit a correction.
                with _project_lock(session_id):
                    if (history and history[-1].get("role") == "assistant" and
                            history[-1].get("content") == text):
                        history[-1] = {"role": "assistant", "content": correction}
                st["continuation_instruction"] = (
                    "CONTROLLER CORRECTION: no approval is pending. The design contract "
                    "is locked, and changes limited to pipeline stages, register placement, "
                    "multiplier strategy, memory strategy, or rationale are internal "
                    "optimizations. Continue now with the next required RTL/tool call. "
                    "Do not ask the user for approval and do not stop at a readiness summary."
                )
                logger.warning("[%s] suppressed stale approval request and continued", session_id)
                yield {"type": "status", "content": correction}
                yield _structured_event(
                    "decision", "contract", "Ignored stale approval request", "done",
                    reason=correction,
                    attempts=_event_attempts(st, round_no=round_no),
                    budgets=_event_budgets(st, round_no=round_no),
                )
                if streamed:
                    yield {"type": "text", "content": correction}
                continue
            logger.info("[%s] agent: %s", session_id, _truncate(text, 500))
            # Streamed text has already been forwarded as text_delta frames;
            # emitting a second full text frame would duplicate it in the UI.
            if not streamed:
                yield {"type": "text", "content": text}
            return

        # Some OpenAI-compatible providers stop after private reasoning when
        # the completion budget is exhausted. This is recoverable once, but
        # must never become an unbounded paid retry loop.
        length_reasons = {"length", "max_tokens", "max_completion_tokens"}
        if (finish_reason in length_reasons and
                int(st.get("empty_continuations", 0) or 0) < MAX_EMPTY_CONTINUATIONS):
            st["empty_continuations"] = int(st.get("empty_continuations", 0) or 0) + 1
            st["continuation_instruction"] = (
                "The previous provider generation exhausted its output limit "
                "before returning assistant content or a tool call. Continue "
                "the same user request from the preserved project state now. "
                "Do not repeat private reasoning; emit the next required tool "
                "call or a concise final answer.")
            # A reasoning model would burn the same budget on thinking again;
            # when the model supports effort levels, the retry asks for low.
            st["continuation_kwargs"] = _continuation_request_kwargs(session_id)
            logger.warning(
                "[%s] empty response after provider finish_reason=%s; "
                "requesting bounded continuation %d/%d",
                session_id, finish_reason, st["empty_continuations"],
                MAX_EMPTY_CONTINUATIONS)
            yield {"type": "status", "content": (
                "Model output limit reached; continuing once from the preserved "
                "project state…")}
            yield _structured_event(
                "decision", "run", "Provider output limit reached", "running",
                reason=(f"finish_reason={finish_reason}; no assistant content or "
                        "tool call was committed"),
                attempts=_event_attempts(st, round_no=round_no),
                budgets=_event_budgets(st, round_no=round_no),
            )
            continue

        # A response with neither content nor tool calls is not a successful
        # final answer. Include the provider's finish reason so a token limit,
        # content filter, and genuinely empty response are distinguishable.
        reason_detail = (f" Provider finish_reason={finish_reason}."
                         if finish_reason else " The provider supplied no finish reason.")
        empty_response = (
            "Model returned an empty response without tool calls; the run was "
            "stopped safely." + reason_detail + " Retry the request to continue "
            "from the preserved project state.")
        with _project_lock(session_id):
            history.append({"role": "assistant", "content": empty_response})
        logger.error("[%s] empty model response on round %d (%s)",
                     session_id, round_no, finish_reason or "no finish reason")
        yield _structured_event(
            "decision", "run", "Empty model response", "failed",
            reason=("The provider returned neither assistant content nor tool "
                    "calls." + reason_detail),
            attempts=_event_attempts(st, round_no=round_no),
            budgets=_event_budgets(st, round_no=round_no),
        )
        yield {"type": "error", "content": empty_response}
        return

    logger.warning("[%s] hit MAX_TOOL_ROUNDS (%d)", session_id, MAX_TOOL_ROUNDS)
    st = _get_state(session_id)
    failure = _compact_result("last simulation", str(st.get("last_simulation_failure") or ""), 1200)
    good = st.get("last_good_verilog")
    good_hash = _code_sha256(good)[:16] if isinstance(good, str) and good else "none"
    detail = f" Last failure: {failure}." if failure else ""
    stop = (f"Stopped after {MAX_TOOL_ROUNDS} tool-call rounds without a final answer."
            f"{detail} Known-good simulation checkpoint sha256={good_hash}; "
            "restore it before continuing.")
    yield _stop_decision_event(stop)
    yield {"type": "text", "content": stop}
