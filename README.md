# Autonomous FPGA/ASIC Design Agent

Local engineering software for turning a hardware specification into RTL,
verification evidence, implementation reports, and reproducible artifacts.
The stack combines a FastAPI agent backend, a React engineering console, and
open-source FPGA/ASIC tools.

The repository is local-only and single-user. Authentication, ownership,
origin controls, and resource limits are future work. Bind development servers to localhost.

## Current position

| Area | Status |
| --- | --- |
| Platform | Architecture contracts, durable jobs, evidence APIs, timing optimization, and visualization foundation are implemented. |
| Benchmark | The frozen `systolic_mm_4x4` corpus has repeated functional/protocol qualification and bounded Sky130 implementation evidence. |
| Tiny Tapeout | Local `tt_um_counter8` hardening is clean. Provider submission and acceptance remain future work. |
| Open_MC | RV32IMC baseline, PMP gate, interrupt lifecycle gate, and Sail differential gate are passed for the repaired CV32E40P profile. |
| Physical direction | IHP SG13G2 remains the leading open candidate. Physical refresh and final core selection are open. |

Open_MC targets 250 MHz. A 500 MHz result is a technology-dependent stretch
objective. A Linux-capable RV64 SoC around 1 GHz is a later milestone.
The detailed status and exit criteria live in [docs/roadmap.md](docs/roadmap.md)
and [docs/open-mcu-milestone-definition.md](docs/open-mcu-milestone-definition.md).

## How the evidence chain works

```text
specification -> independent reference -> RTL -> verification
              -> synthesis/STA -> place and route -> reproducible artifacts
```

The verification order is lint, differential testing, assertions, functional
and structural coverage, formal properties, equivalence, CDC/RDC, then
portability. Missing tools keep the affected gate inconclusive. See [verification and evidence contracts](docs/agent-reference/verification-and-evidence.md).

Results are content-addressed and tied to source, contract, constraints, job,
tool, netlist, and artifact identities. The evidence chain supports engineering
decisions and review. Manufacturing signoff requires a separate approved flow.

## Quick start with Docker

Put provider keys in the repository-root `.env`, then run:

```bash
docker compose up -d --build
curl http://127.0.0.1:8001/health
```

Open the console at <http://localhost:3000>. The backend is available at
<http://localhost:8001>. Project data, logs, provider-key state, and job state
are bind-mounted so they survive container rebuilds.

## Quick start on the host

Install a provider and the EDA tier you need, then start the two processes:

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./start.sh                         # http://localhost:8000
```

In another terminal:

```bash
cd frontend
npm install
npm run dev                         # http://localhost:5173
```

`backend/start.sh` uses the required graceful shutdown settings. The frontend
reconnects to the backend WebSocket during local development.

## Model providers

The project registry exposes three provider families. Keys belong in the
root `.env`, in an ignored shell environment, or in the server-side provider
store. Never commit them.

| Provider | Environment | Default use |
| --- | --- | --- |
| Kimi direct | `KIMI_API_KEY` | `kimi-k3` |
| OpenRouter | `OPENROUTER_API_KEY` | OpenRouter-hosted Kimi models |
| Mistral | `MISTRAL_API_KEY` | `mistral-vibe` and Mistral-hosted GLM |

Optional model and endpoint overrides are documented in
[`backend/.env.example`](backend/.env.example). Full provider behavior is
covered by [the backend reference](docs/agent-reference/backend.md).

```bash
KIMI_API_KEY=...
OPENROUTER_API_KEY=...
MISTRAL_API_KEY=...
```

## EDA tiers

The backend detects available tools for each run and records the selected tier.

| Tier | Tools | Evidence |
| --- | --- | --- |
| 1 | Yosys, Verilator or Icarus | Lint, simulation, synthesis |
| 2 | Tier 1 plus OpenSTA | Constrained timing and Fmax search |
| 3 | Tier 2 plus OpenROAD and KLayout | Place and route, layout and physical reports |

Use [scripts/install-local-eda.sh](scripts/install-local-eda.sh) for a no-root
setup. Operational details, `SCPATH`, `SC_BUILDS`, constraints, and Docker
behavior are in [docs/agent-reference/eda-and-operations.md](docs/agent-reference/eda-and-operations.md).

Sky130 and open PDK runs are screening or engineering evidence. Manufacturing
candidate and commercial/foundry signoff require the appropriate PDK, libraries,
rule decks, approved flow, and independent review.

## Open_MC gate summary

The product repository is private and owns Open_MC implementation releases.
This repository stores qualification evidence bound to exact revisions.

- ACT4 RV32IMC, Zicsr, and Zifencei: 80/80, reproduced byte-identically.
- ACT4 privileged PMP: 45/45 twice. PMPSm: 33/33. PMPZca: 12/12.
- Interrupt lifecycle evidence includes the complete 20/20 fast-line sweep and
  4/4 controlled two-level nesting campaign.
- Sail differential: 16 deterministic seeds × 128 operations passed twice with
  byte-identical manifests against Sail 0.13.1 from the pinned ACT4 image.
- Seeded synchronous faults: 12/12 across four seeds with independently checked
  `mcause`, `mepc`, `mtval`, trap count and `mret` recovery.
- Open gates: seeded PMP and interrupt campaigns, bus-error and compressed-fault
  coverage, refreshed physical evidence, and final core selection.

Evidence records are indexed under
[`docs/evidence/open-mcu-*.json`](docs/evidence/) and the milestone contract
is [docs/open-mcu-milestone-definition.md](docs/open-mcu-milestone-definition.md).

## Tests

```bash
cd backend && .venv/bin/python -m pytest -q
cd frontend && npm run lint && npm run build && npm run test:browser
cd frontend && npm run test:browser:perf
git diff --check
```

The frozen matmul qualification uses
[`scripts/run-matmul-qualification.py`](scripts/run-matmul-qualification.py).
The current qualification ledger and known limitations are in the roadmap.

## Documentation index

- [Roadmap and dated status](docs/roadmap.md)
- [Backend contracts and runtime](docs/agent-reference/backend.md)
- [Frontend contracts and UX](docs/agent-reference/frontend.md)
- [EDA and operations](docs/agent-reference/eda-and-operations.md)
- [Verification, evidence, and assurance](docs/agent-reference/verification-and-evidence.md)
- [Matmul qualification audit](docs/s8-matmul-qualification-audit.md)
- [Tiny Tapeout guide](docs/tapeout.md)
- [Open_MC milestone definition](docs/open-mcu-milestone-definition.md)
- [Phase 3 physical gap audit](docs/phase3-physical-gap-audit.md)

Screenshots and short workflow captures will be added here after the console
surface stabilizes.
