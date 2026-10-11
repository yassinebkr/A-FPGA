# Autonomous FPGA/ASIC Design Agent

A full-stack system for an autonomous hardware-engineering agent. It turns a hardware intention into Verilog, verifies it (lint, simulation, assertions, coverage, formal, CDC/RDC), runs synthesis and timing analysis, and presents PPA evidence through a web UI.

The long-term objective is manufacturable hardware; the agent-generated Verilog is a means toward it. The current SiliconCompiler demo flows serve engineering exploration, and silicon signoff requires a vendor-approved flow (see [Assurance levels](#assurance-levels)).

The active platform milestone is an open, modifiable 32-bit RISC-V microcontroller taken through fabrication, packaged-silicon bring-up and documented revisions. It qualifies the platform end to end and has a dedicated private product repository, Open_MC, from the first implementation revision. This repository remains the autonomous FPGA/ASIC design platform. The milestone targets 250 MHz, with 500 MHz retained as a technology-dependent stretch objective. A Linux-capable RV64 SoC around 1 GHz is a future milestone after this complete silicon loop.

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Quick start (Docker)](#quick-start-docker)
- [Quick start (host)](#quick-start-host)
- [Model providers](#model-providers)
- [EDA toolchain](#eda-toolchain)
- [Verification and evidence](#verification-and-evidence)
- [Assurance levels](#assurance-levels)
- [Current status](#current-status)
- [Tests](#tests)
- [Documentation index](#documentation-index)

## What it does

Every design follows one auditable evidence chain:

```text
Explicit specification
  -> independent reference model
  -> generated RTL
  -> lint, simulation, assertions, coverage, formal and CDC/RDC checks
  -> synthesis and place-and-route
  -> timing, resource and power reports
  -> reproducible FPGA or manufacturing artifact package
```

The backend is one rigorous engineering pipeline; the frontend exposes it at three levels:

| Level | Exposed |
|---|---|
| Beginner | Intent-driven chat, safe defaults, explanations, guided verification |
| Intermediate | Editable RTL/testbenches, clock/device constraints, goals, diagnostic reports |
| Expert | Full constraints, critical paths, utilization, power assumptions, tool versions, logs, reproducibility metadata |

These are UI layers over one backend. They must not change the underlying evidence or hide assumptions from expert users.

## Architecture

- **Model providers** — project-selectable OpenAI-compatible backends resolved through `backend/model_registry.py` (see [Model providers](#model-providers)).
- **Backend** — FastAPI (Python), WebSocket chat, direct tool-calling loop, JobManager-backed asynchronous EDA jobs with live logs and cancel.
- **Frontend** — React 19 + Vite + Tailwind CSS 4 + shadcn/ui, dark engineering-console design.
- **EDA** — SiliconCompiler (Yosys, Verilator, OpenSTA, OpenROAD, KLayout); storage via the `SCPATH` / `SC_BUILDS` env vars (see `backend/.env.example`).

```text
backend/     FastAPI server + agent logic + EDA tools (flat module layout)
frontend/    React/Vite SPA
docs/        Plans, roadmap, tapeout guide, audits
scripts/     Installers and acceptance/qualification scripts
tapeout/     Tiny Tapeout workspace (gitignored, template not committed)
```

## Quick start (Docker)

The supported deployment: one backend+EDA image built from SiliconCompiler's pinned runner image, frontend behind nginx.

```bash
# Root .env must hold KIMI_API_KEY (compose reads it automatically;
# the key is never baked into the image).
docker compose up -d --build
curl http://127.0.0.1:8001/health     # backend (host port 8001)
# UI: http://localhost:3000
```

`backend/projects`, `backend/logs`, and `backend/data` are bind-mounted, so history, the job registry, and wizard-saved keys survive rebuilds and `docker compose down/up`.

## Quick start (host)

**1. Configure at least one model provider** — see [Model providers](#model-providers).

**2. Install the EDA tools** — see [EDA toolchain](#eda-toolchain) (a lint/synthesis-only setup works too; the backend fails closed on missing tools rather than pretending).

**3. Start the backend** (first time: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`):

```bash
cd backend
./start.sh    # uvicorn --reload --timeout-graceful-shutdown 3 --host 0.0.0.0 --port 8000
```

The graceful-shutdown flag is mandatory: the frontend auto-reconnects its WebSocket every 2 s, which otherwise hangs uvicorn's shutdown and orphans the port.

**4. Start the frontend** (new terminal):

```bash
cd frontend
npm install && npm run dev
```

**5. Use it** — open the frontend (the status pill shows backend health) and issue commands like:

> "Build an INT8 MAC array and run synthesis."

Everything is logged to console and a rotating `backend/logs/backend.log` (10 MB x 5 backups), session-tagged, so any run can be reconstructed after a crash.

## Model providers

The UI lists the registered models, disables entries whose API key is absent, and lets each project select its model.

| Registry key | Provider | Key env var | Notes |
|---|---|---|---|
| `kimi-k3` | Kimi Code API (default for legacy projects) | `KIMI_API_KEY` | `KIMI_MODEL` / `KIMI_BASE_URL` overrides |
| `mistral-vibe` | Mistral platform API | `MISTRAL_API_KEY` | Same connection flow as [mistralai/mistral-vibe](https://github.com/mistralai/mistral-vibe); default model `mistral-medium-3-5`; `MISTRAL_MODEL` / `MISTRAL_BASE_URL` overrides |
| `mistral-glm-5.3` | GLM 5.3 hosted by Mistral | `MISTRAL_API_KEY` | Model id `zai-glm-5-3`, same platform API; a reasoning model that always thinks — effort selectable per project in the UI (default `low`; the run token budget scales with the effort: x1/x2/x4 for low/high/max) |

Provider reasoning effort is a per-project setting (the selector appears next to the model dropdown for models that declare effort levels); `GLM_REASONING_EFFORT` in `.env` overrides the machine-wide default.
| `openrouter-kimi-k2.7-code` | OpenRouter | `OPENROUTER_API_KEY` | `moonshotai/kimi-k2.7-code` |
| `openrouter-kimi-k3` | OpenRouter | `OPENROUTER_API_KEY` | `moonshotai/kimi-k3` |

Set keys in the repository-root `.env` (or export them in the shell that starts uvicorn):

```bash
KIMI_API_KEY=your-direct-kimi-key
OPENROUTER_API_KEY=your-openrouter-key
MISTRAL_API_KEY=your-mistral-key
```

Precedence: shell exports > root `.env` > `backend/.env` (legacy fallback). Never commit keys.

### Mistral setup wizard

Instead of pasting a key by hand, run the interactive wizard — the same PKCE browser sign-in as `vibe --setup`:

```bash
cd backend && .venv/bin/python mistral_setup.py
```

1. Choose *Sign in with your browser* (or paste a key from console.mistral.ai).
2. It opens `console.mistral.ai/codestral/cli/authenticate` and waits; custom login domains work like Vibe's "Other" step (enter the bare domain, the auth base is derived as `DOMAIN/api`).
3. The backend exchanges the flow for an API key server-side and verifies it with the free `/v1/models` probe. The key never travels to the browser.

Saving a key has three effects:

- written into the `.env` the backend loads;
- mirrored into the durable store `backend/data/provider_keys.env` — bind-mounted in docker-compose, so it survives `docker compose down/up` (the image-layer `.env` does not);
- exported into the running process, so the backend adopts it **without a restart**.

The same wizard is available from the web UI: a **mistral setup** button appears next to the model selector whenever a Mistral entry has no key.

REST surface (for the UI): `GET /api/providers/mistral/setup/state`, `POST .../browser/start`, `GET .../browser/status`, `POST .../browser/complete`, `POST .../key`.

Provider health checks use free authenticated metadata endpoints and never issue a paid completion. The registry is extensible: another OpenAI-compatible provider can be added without touching the chat loop.

## EDA toolchain

The backend picks the deepest flow the installed tools support and fails closed on missing tools:

| Tier | Tools | Flow |
|---|---|---|
| 1 | yosys (+ verilator/iverilog for simulation) | lint, simulation, synthesis |
| 2 | + OpenSTA (`sta` binary) | + constrained timing, `find_fmax` search |
| 3 | + openroad + klayout | + full place & route, layout view |

Reproducible no-root setup (pinned, source-built, under the gitignored `.tools/eda`; `backend/start.sh` adds its `bin` to `PATH`):

```bash
./scripts/install-local-eda.sh             # Verilator 5.040, Icarus 12.0 (simulation)
./scripts/install-local-timing-runtime.sh # Tcl 8.6.16 runtime (install this first)
./scripts/install-local-formal.sh         # Yosys 0.50, SymbiYosys 0.44, Z3 4.13.4
nix --extra-experimental-features 'nix-command flakes' profile add nixpkgs#openroad
./scripts/install-klayout.sh              # KLayout 0.30.12 via the same nix profile
```

Alternative system-wide install on Debian 13 (Tier 1, plus OpenSTA from source; Debian ships no CUDD package, so it is built first below):

```bash
sudo apt install yosys verilator klayout
git clone https://github.com/ivmai/cudd.git /mnt/cold-hdd/cudd && cd /mnt/cold-hdd/cudd
autoreconf -fi && ./configure --prefix=/mnt/cold-hdd/cudd/install && make -j$(nproc) && make install
git clone https://github.com/The-OpenROAD-Project/OpenSTA.git /mnt/cold-hdd/OpenSTA && cd /mnt/cold-hdd/OpenSTA
cmake -B build -DCMAKE_BUILD_TYPE=Release -DCUDD_DIR=/mnt/cold-hdd/cudd/install && cmake --build build -j$(nproc)
echo 'export PATH=/mnt/cold-hdd/OpenSTA/build:$PATH' >> ~/.bashrc
```

Operational notes:

- Tools must be on the PATH of the shell that starts uvicorn; the backend re-detects them on every synthesis call.
- For `iverilog`/`yosys` subprocesses, `LD_LIBRARY_PATH` must include `.tools/eda/lib` (the source-built Tcl runtime) — and must **not** point at an editor's bundled `libstdc++`, which breaks both binaries.
- Clocked designs get an auto-generated `<top>.sdc` (`create_clock` + uncertainty + 20% I/O delays) so setup/hold slack metrics are real; synthesis runs only after a self-checking testbench passes.
- A full P&R additionally produces the digest-bound layout view (routed DEF: die, rows, placed cells mapped to gate-schematic ids, IO pins). GDS stays downloadable evidence and is never parsed in the browser thread.

### Fmax evidence

`find_fmax` searches the feasible clock period with synthesis+STA probes (default bracket 5–40 ns, refinement to 0.5 ns or a 12-run budget including one confirmation run). Results report the selected period, measured setup WNS, and explicit bounds/uncertainty/assumptions. Frequency bounds are the reciprocal conversion of the period interval. Read them as a feasibility interval under the stated assumptions; board-level and silicon timing come from the device/PDK-specific flow.

Acceptance gate (real SC flow; passes only when the estimate is confirmed, monotonic, and within tolerance):

```bash
bash -o pipefail -c './scripts/run-fmax-acceptance.py 2>&1 | tee fmax-acceptance.log'
```

## Verification and evidence

One ordered pipeline behind the `run_verification` gate: `lint -> differential -> assertions -> functional coverage -> structural coverage -> formal properties -> equivalence -> CDC/RDC -> portability`. Mandatory gates come from the project's `DesignSpec`; when a tool is missing, its gate stays at `inconclusive`.

Durable, versioned evidence artifacts:

- versioned `DesignSpec` proposals and an approved `ArchitectureContract` (content-hashed; material revisions invalidate stale RTL/evidence checkpoints);
- immutable `RunManifest` and derived `EvidenceReport` records under `/api/evidence`, artifacts content-addressed by SHA-256;
- independent human-authored Python reference models (`alu8`, `counter8`, `arbiter4`, and the frozen `systolic_mm_4x4` qualification corpus) with directed + seeded-random vectors;
- typed constraints rendered to authoritative SDC/XDC, byte-hash verified at the subprocess boundary;
- structural CDC/RDC analysis (domain discovery, two-flop synchronizer recognition, expiring waivers) — a conservative structural review; commercial CDC signoff remains a separate, dedicated process;
- a strict board-neutral FPGA profile contract and a hardware verification gate that fails closed when hardware is requested but unavailable.

## Assurance levels

The project distinguishes three levels and never blurs them:

| Level | Meaning |
|---|---|
| Screening / estimate | Bounded synthesis+STA evidence from declared open-source technology inputs, currently the Sky130 demo flow and the provisional IHP SG13G2 typical-corner profile |
| Manufacturing candidate | The configured checks, independent reference model, formal/equivalence, CDC/RDC review, implementation reports, and (when available) FPGA hardware-in-the-loop evidence produced a reproducible package with no known failures. This is an engineering result; the signoff level below covers the residual silicon risks. |
| Commercial/foundry signoff | The shuttle or vendor's approved PDK, libraries, rule decks, and signoff flow, with independent review. This repository must not label a demo-PDK estimate or open-source structural check as signoff. |

Intended path:

```text
Agent RTL
  -> independent verification and open-source implementation evidence
  -> FPGA board / hardware-in-the-loop validation
  -> manufacturing-candidate review and reproducible artifact package
  -> shuttle/foundry-specific checks and signoff
  -> shared-wafer or Tiny Tapeout submission
```

The shared-wafer route reaches manufacturing through the same final checks. Tiny Tapeout preparation follows the same principle — see [docs/tapeout.md](docs/tapeout.md).

## Current status

The detailed ledger — phases, exit criteria, measured numbers, honest limitations — lives in [docs/roadmap.md](docs/roadmap.md). Summary:

- **Implemented**: asynchronous jobs with live logs and cancel; architecture-first generation with a persistent hashed contract; evidence-backed timing optimization; durable jobs/dashboard/layout surfaces; the verification pipeline and evidence APIs above; provider streaming and the Mistral wizard.
- **Qualified benchmark**: the frozen `systolic_mm_4x4` contract passes three independent 40-vector functional/protocol/latency trials, a 20 ns Sky130 screening run (+0.34 ns slack, 50.86 MHz), and one LibreLane full P&R run with clean DRC/LVS/XOR/antenna gates and positive multi-corner slack. It retains electrical-rule warnings and no post-layout equivalence, so the package qualifies as routed engineering evidence; signoff remains a later step. Exact figures: `docs/roadmap.md`.
- **Tiny Tapeout**: a local Sky130 hardening run of `tt_um_counter8` completed clean (LibreLane 3.0.14, 80/80 stages); cloud submission and shuttle acceptance are future manual steps — see [docs/tapeout.md](docs/tapeout.md).
- **Open_MC milestone**: the separate product repository has a locked RV32IMC MCU contract, pinned CV32E40P/CVE2 candidates, reviewed provenance, common measurement wrappers, native elaboration, generic synthesis, and IHP SG13G2 timing screens. Characterized ABC repair closes 100 MHz at +1.6612 ns. The matched official-PDK OpenROAD matrix closes 100 MHz for CV32E40P (+0.3213 ns) and CVE2 (+0.1664 ns). At 150 MHz both miss: CV32E40P measures -0.9031 ns and 127.0519 MHz estimated placed Fmax; CVE2 measures -1.0953 ns and 123.0456 MHz. CVE2 uses 10.28% less placed cell area at 150 MHz. The results are pre-CTS and pre-route, and CV32E40P remains the provisional timing baseline. The untouched-core control passes 5/8 cases on CV32E40P and 7/8 on CVE2. Three owned, hash-bound CV32E40P patches propagate response errors, enforce strict load/store alignment, and enable eight machine-mode PMP regions. The expanded thirteen-case corpus passes 13/13 twice, including precise causes 1/4/5/6/7, region 7 lock persistence, and pre-transfer rejection. A pinned legacy RV32I regression now passes 44/48 with exactly four declared suite exclusions, zero unexpected failures, and byte-identical manifests across two hardened runs. Both wrappers use one additional HPM counter. CV32E40P is the implementation candidate and CVE2 remains the control; official RISCOF, randomized differential verification, and refreshed physical evidence remain required. Core selection, product RTL, functional qualification, memories, IO, architecture optimization, multi-corner timing, and physical closure remain open. Durable evidence is in `docs/evidence/open-mcu-*.json`.

Open_MC timing convergence combines three measured levers: optimize the selected
core configuration and synthesis flow while preserving every product feature;
cut proven critical paths through reviewed pipeline changes; then qualify faster
standard-cell/library or process options where the provider-supported IHP stack
cannot sustain the target. The 250 MHz objective stays a product requirement and
each lever receives its own functional, timing, area, power, and physical evidence.

Qualification commands (no LLM involved; artifacts under `backend/data/qualifications/matmul3/`, gitignored):

```bash
./scripts/run-matmul-qualification.py                                    # validate frozen spec + corpus
./scripts/run-matmul-qualification.py --rtl path/to/rtl.sv --dry-run    # validate a candidate, no EDA
./scripts/run-matmul-qualification.py --rtl path/to/rtl.sv --trials 3  # RTL/protocol regression
./scripts/run-matmul-qualification.py --rtl path/to/rtl.sv --trials 3 --timing   # + 20 ns Sky130 screen
```

Authentication, project ownership, and network-facing limits are intentionally deferred while the deployment stays local and single-user. They are required before any network deployment.

## Tests

```bash
cd backend && .venv/bin/python -m pytest tests/   # backend contract suite
cd frontend && npm run lint && npm run build      # oxlint + production build
cd frontend && npm run test:browser                # browser suite (mocked backend)
```

## Documentation index

| Document | Content |
|---|---|
| [docs/roadmap.md](docs/roadmap.md) | Engineering status ledger, phases, exit criteria, measured numbers |
| [docs/tapeout.md](docs/tapeout.md) | Tiny Tapeout wrapper contract and hardening steps |
| [docs/fpga-profiles.md](docs/fpga-profiles.md) | FPGA profile contract |
| [docs/jobs-workbench-implementation-plan.md](docs/jobs-workbench-implementation-plan.md) | Jobs workbench plan and completion log |
| [docs/workbench-density-and-gate-visualization-plan.md](docs/workbench-density-and-gate-visualization-plan.md) | Gate-level viewer plan (D1–G4) and completion log |
| [docs/open-mcu-milestone-definition.md](docs/open-mcu-milestone-definition.md) | Locked Open_MC product contract and frequency strategy |
| [docs/evidence/open-mcu-core-ihp-typical-screen.json](docs/evidence/open-mcu-core-ihp-typical-screen.json) | Latest reproducible provisional IHP core timing screen |
| [AGENTS.md](AGENTS.md) | Full architecture and module-by-module reference |
