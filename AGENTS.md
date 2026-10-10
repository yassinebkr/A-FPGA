# AGENTS.md — Autonomous FPGA/ASIC Design Agent

## Communication style

- State conclusions and corrections directly.
- Avoid contrastive correction formulas shaped as “X, not Y” or “X, pas Y”,
  including close variants that negate one label merely to replace it with
  another.
- Keep operational updates concise and proportional to the work.

## Project identity

This repository is a local, single-user full-stack agent for generating,
verifying, synthesizing, and presenting FPGA/ASIC RTL. The frontend is React
19/Vite/Tailwind/shadcn. The backend is FastAPI with a direct OpenAI-compatible
tool-calling loop, durable projects/evidence, and asynchronous EDA jobs. The
EDA boundary is SiliconCompiler with Yosys, Verilator, OpenSTA, OpenROAD, and
KLayout selected from the environment.

Repository areas:

```text
backend/    FastAPI, agent loop, verification, evidence, EDA adapters
frontend/   React/Vite engineering-console SPA
docs/       plans, qualification reports, evidence references, agent details
scripts/    installers, qualification runners, preservation utilities
tapeout/    gitignored third-party Tiny Tapeout workspace
```

## Required routing

Read the relevant reference before changing that area. The reference files
contain the detailed module contracts and operational rules that previously
lived in this file.

- [Backend contracts and runtime](docs/agent-reference/backend.md)
- [Frontend contracts and UX](docs/agent-reference/frontend.md)
- [EDA, Docker, Tiny Tapeout, and operations](docs/agent-reference/eda-and-operations.md)
- [Verification, evidence, assurance, and qualification](docs/agent-reference/verification-and-evidence.md)

## Change discipline

- Preserve existing user changes, persisted project data, evidence, and runtime
  state. Inspect a dirty tree before editing overlapping files.
- Use small, reviewable commits for one implementation phase or one verified
  fix. Record exact checks and remaining criteria in the active plan.
- Preserve schema versions and public API shapes unless a migration is
  deliberate and covered by compatibility tests.
- Never commit `.env` files, provider keys, virtual environments, runtime
  databases, logs, persisted projects, `node_modules`, frontend build output,
  `.tools`, or the third-party `tapeout` workspace. Check `.gitignore` before
  staging.
- Avoid destructive Git operations, history rewriting, force pushes, broad
  deletion, or guessed remotes without explicit owner approval.
- Use `apply_patch` for repository edits. Keep generated/runtime output in
  ignored locations.
- Deterministic implementation and qualification work must not invoke paid
  model calls unless the owner explicitly requests that experiment. Avoid new
  EDA runs when existing durable evidence satisfies the phase.

## Essential checks

```bash
cd backend && .venv/bin/python -m pytest -q
cd frontend && npm run lint && npm run build && npm run test:browser
cd frontend && npm run test:browser:perf
git diff --check
```

The browser suite uses mocked backend routes and the current count is reported
by the active plan. A controlled C5 performance run is required for the
interaction gate; green unit checks alone cannot close the phase. The current
controlled run is recorded in the active plan.

## Security and execution boundary

The application remains local and single-user. Authentication, ownership,
origin controls, resource limits, and network deployment are deferred work.
The backend currently binds for local development and has wide-open CORS;
keep that boundary visible in documentation and never present it as a safe
network deployment.

Secrets stay in ignored environment files or the server-side provider-key
store. Never print, return, or commit provider keys. `SCPATH` and `SC_BUILDS`
define EDA storage; validate model-controlled names and paths beneath the
configured build root.

## Current phase rule

The visualization consolidation plan and Part 6 are complete through C6 for
the bounded qualification scope. The controlled memoized SVG/minimap run
closes the post-load 100 ms interaction gate: zoom is 48 ms and the maximum
post-load long task is 58 ms. Five load-phase long tasks were recorded, with
296 ms as the maximum; they remain separately reported startup observations.

The local Git baseline begins on 2026-10-10. No remote or pre-baseline history
is assumed. See the active plan and roadmap before declaring a phase complete.
