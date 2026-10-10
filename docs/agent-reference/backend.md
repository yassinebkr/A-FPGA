# Agent reference — backend

This document contains the backend rules routed from the repository
`AGENTS.md`. Preserve the existing public contracts and use focused tests for
the touched boundary.

## Runtime and WebSocket contract

`backend/main.py` owns the FastAPI application and lifespan. `GET /health`
reports model availability and cached health. `/ws` accepts project selection,
plain-text chat, cancellation, job subscription, and job cancellation frames.

Incoming chats run in per-message tasks and serialize per project/session via
the `_SESSIONS` lock. A second message for a busy session receives an explicit
error and `end`; independent sessions may run concurrently. Chat frames are
published to a bounded per-session event buffer with sequence numbers and wall
clock timestamps. Rejoining a session receives history, busy state, buffered
events without duplicates, then live events. All outbound frames use one
per-connection outbox sender so the receive loop remains responsive during
tool jobs.

The protocol retains legacy `text`, `tool_call`, `tool_result`, `error`, and
`end` frames. It also supports `generation_start`, `text_delta`, provider
`reasoning_delta`, `generation_usage`, `generation_end`, `status`, project
state, job-start, job-status, tool-log, and log-gap frames. Tool-call content
is JSON containing `name` and `args`.

Disconnects leave an active chat task running server-side and retain it in the
detached-task set. A cancel frame locates the active task through the bound
session and cancels it across event loops; the cancellation event is emitted
to all clients watching that project.

Project REST endpoints persist JSON under `backend/projects`: list/create,
rename/model/reasoning-effort patch, delete, model listing, and Mistral setup.
Project model changes validate registry keys and supported reasoning effort.
Project join state restores persisted Verilog, metrics, lint binding, and
active run state. Mistral setup keeps exchanged keys server-side and exposes
only state/control endpoints.

The REST surface includes `GET /api/models`, `GET /api/jobs`,
`GET /api/providers/mistral/setup/state`, browser start/status/complete, and
key submission endpoints. Project creation is `POST /api/projects`; updates
use `PATCH /api/projects/{id}` and deletion uses `DELETE /api/projects/{id}`.

## Jobs and persistence

`backend/jobs.py` is the asynchronous JobManager. Each submission owns an
asyncio task and a process group created with `start_new_session=True`. Long
EDA commands stream line-by-line into a bounded in-memory ring and a durable
`backend/data/jobs/<job_id>/job.log`; slow subscribers receive a `log_gap`
marker. Jobs carry project/session ownership, survive WebSocket disconnects,
and are listed by `GET /api/jobs`.

States are `queued`, `running`, `done`, `failed`, `cancelled`, and
`interrupted`. Cancellation terminates the process group, escalates after the
grace period, and always reaps the child. Startup reconciliation marks stale
queued/running rows interrupted. SQLite migrations preserve old job tables;
`JobManager.stop()` is mandatory so aiosqlite worker threads terminate.

The cancellation path handles `CancelledError`, sends SIGTERM to the process
group, then SIGKILL after the grace period. Startup migration checks
`PRAGMA table_info` before `ALTER TABLE ADD COLUMN`. Live frame names are
`job_started`, `tool_log`, `job_status`, and `log_gap`; job logs carry their own
sequence cursor.

`project_repository.py` uses per-project re-entrant locks, same-directory
temporary files, fsync, atomic replace, and directory fsync. Corrupt files are
isolated. `evidence.py` and `evidence_api.py` persist immutable manifests and
content-addressed artifacts by SHA-256.

## Agent loop and providers

`backend/agent_core.py` resolves the model per project/session through
`model_registry.py`. The loop uses OpenAI-compatible tools, a bounded maximum
of ten rounds, explicit LLM timeout/connect/inactivity/overall deadlines, and
provider streaming. Fragmented tool calls are assembled before invocation.
Provider reasoning is exposed through the folded live reasoning channel and
bounded status heartbeats. Reasoning models receive their larger completion
budget; a single bounded empty-length continuation may use low reasoning effort
when the model declares that capability. Empty responses remain explicit
errors. Missing API keys produce an error frame.

Tool invocation sets the session context so EDA jobs are tagged with their
project. Repair policy tracks syntax failures, total tool failures, last
simulation-passing RTL, and accumulated usage. Syntax and total-repair caps
produce a change-strategy notice and preserve the last passing checkpoint.
Every tool result carries honest remaining-token or session-usage information.

Registry entries currently cover direct Kimi K3, Mistral platform models,
Mistral-hosted GLM 5.3, and OpenRouter Kimi K2.7 Code/K3. Health probes use
free authenticated metadata endpoints, cache briefly, and never make a paid
completion call.

Per-project `reasoning_effort` is validated against the registry capability and
persisted through the project PATCH surface. Unsupported efforts are rejected
before a chat starts.

Exact registry keys are `kimi-k3`, `mistral-vibe`, `mistral-glm-5.3`,
`openrouter-kimi-k2.7-code`, and `openrouter-kimi-k3`. Environment overrides
are `KIMI_API_KEY`, `KIMI_MODEL`, `KIMI_BASE_URL`, `MISTRAL_API_KEY`,
`MISTRAL_MODEL`, `MISTRAL_BASE_URL`, `OPENROUTER_API_KEY`,
`GLM_REASONING_EFFORT`, and `LOG_LEVEL`. Probe paths are
`GET {base_url}/models`, `GET /v1/models`, and `GET /auth/key`; the health
cache is controlled by `HEALTH_TTL_S`.

The loop uses `AsyncOpenAI`. Context admission uses `MODEL_CONTEXT_MAX_CHARS`;
reasoning bounds use `REASONING_OUTPUT_TOKEN_LIMIT`,
`MAX_EMPTY_CONTINUATIONS`, and `STATUS_HEARTBEAT_S`. Repair caps are
`SYNTAX_REPAIR_CAP` and `TOTAL_REPAIR_CAP`. Cached lint/simulation records use
`TOOL_CACHE_DB`, include `cached: true`, and cached simulations carry no live
`job_id`.

Optimization/controller invariants remain active: a passing simulation is the
checkpoint for synthesis; contract and RTL digests bind timing evidence;
promotion tokens bind one full P&R attempt to the exact screened source;
`FULL PNR SOURCE MISMATCH` and `FULL PNR TOKEN UNKNOWN` return corrective tool
results with the re-earn path. Persisted candidate lineage and promotion state
survive reload, and a durable simulation transcript seeds last-good RTL when
the active lineage is absent.

`logging_config.py` writes session-tagged console and rotating file logs.
`mistral_setup.py` implements the PKCE browser/paste-key flow, validates the
free probe, writes the ignored `.env` and durable provider-key store atomically,
and exports the key to the running process without returning it to the client.

## Backend hardening modules

- `eda_workspace.py` validates portable identifiers, targets, clock periods,
  symlink-aware build containment, and subprocess-boundary paths.
- `genui.py` owns the restricted architecture proposal/approval protocol;
  app-owned components expose typed decisions and digest-bound approval.
- `lint.py` normalizes slang, Verilator, Icarus, and Yosys diagnostics into a
  bounded structured report. `tool_cache.py` caches only lint and simulation
  results using tool-version/content digests.
- `golden_models.py`, `differential_regression.py`, `verification_executor.py`,
  `formal_toolchain.py`, `formal_executor.py`, `cdc_rdc.py`, and
  `verification_policy.py` implement the verification infrastructure described
  in the verification reference.
- `gate_netlist.py`, `timing_paths.py`, and `physical_layout.py` remain public
  compatibility facades over their extracted contract/parser/query layers.

## Backend test policy

Automated tests live in `backend/tests/` and are selected by `backend/pytest.ini`.
Root-level `backend/test_*.py` files are manual live-server/API-key probes and
must remain outside normal collection. Use the full suite for phase acceptance
and focused suites for a changed contract.
