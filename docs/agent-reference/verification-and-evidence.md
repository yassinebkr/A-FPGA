# Agent reference — verification, evidence, and assurance

## Verification order

The ordered verification pipeline is:

```text
lint -> differential -> assertions -> functional coverage -> structural coverage
     -> formal properties -> equivalence -> CDC/RDC -> portability
```

The project `DesignSpec` controls mandatory gates. Missing tools produce
`inconclusive` evidence and cannot silently become passes. Formal and
equivalence outcomes are `proven`, `failed`, `bounded`, or `inconclusive`.
CDC/RDC is conservative structural evidence and remains separate from
commercial CDC signoff. Coverage, formal, CDC/RDC, production lint policy,
and generated-design regression qualification remain broader release work.

A `legacy_directed_regression` result records a pinned historical architectural suite with explicit exclusions. It strengthens bounded regression evidence and remains a separate assurance class from current ACT4 architectural certification. A bounded `act4_product_profile_probe` records official ACT4 self-checking ELF execution against the owned, hash-bound product UDB/Sail configuration. It becomes broader architectural-certification evidence only after the required extension set is executed and reviewed. New failures, missing references, provenance mismatches, and dirty tracked inputs fail closed.

Independent human-authored reference models exist for `alu8`, `counter8`,
`arbiter4`, and the frozen `systolic_mm_4x4` corpus. Differential regression
binds vectors to RTL and records compile/runtime/reference/mismatch outcomes.
Assertions, coverage, formal, equivalence, CDC/RDC, constraints, and reports
are content-addressed and tied to source/contract/tool identities.

## Visualization evidence

`gate-netlist/v1` is canonical, digest-sealed, and derived from structured
Yosys JSON. Bounded graph queries enforce module/depth/cone/node limits.
`timing-paths/v1` is derived from ordered OpenSTA reports with setup/hold,
corner, clock, arrival/required/slack, ordered segments, and explicit partial
mapping. `layout-view/v1` is derived from routed DEF/DEF.GZ with die/rows,
placed cells, pins, nominal footprints, truncation, and partial mapping.
GDS stays downloadable evidence and is not parsed in the browser thread.

Each artifact verifies its own digest and carries project, source, constraint,
job, tool, netlist, and artifact identity. REST endpoints reject mismatched
digests with a clear stale/incompatible response. Restart tests use fresh
JobManager and evidence repository instances against durable roots.

Stable endpoints are `GET /api/evidence`, `GET /api/jobs/{id}/netlist`,
`GET /api/jobs/{id}/netlist/graph`,
`GET /api/jobs/{id}/timing-paths?netlist_sha256=`, and
`GET /api/jobs/{id}/layout?netlist_sha256=`. Durable job columns include
`netlist_sha256`, `timing_paths_sha256`, and `layout_view_sha256`.

## Assurance levels

- Screening/estimate: bounded synthesis and STA from an open Sky130 demo-PDK,
  explicitly labelled `screening` or `estimated`.
- Manufacturing candidate: reproducible package with configured checks,
  independent references, formal/equivalence, CDC/RDC, implementation
  reports, and hardware evidence when available.
- Commercial/foundry signoff: vendor-approved PDK, libraries, rule decks,
  signoff flow, and independent review.

The phrase `commercial/foundry signoff` is reserved for that approved-flow
case and cannot describe demo-PDK or open-source screening evidence.

The local Tiny Tapeout hardening and shared-wafer preparation remain engineering
steps. They do not establish provider acceptance, foundry signoff, or a
commercial signoff package.

## Qualification records

The active visualization plan records C0–C6 checks and remaining limits.
Current C5 evidence includes a synthetic 100,000-cell backend profile and a
150,000-cell browser fixture with bounded 500/1,000-node responses. The server
and heap bounds pass. The controlled memoized SVG/minimap run closes the
post-load 100 ms interaction budget: zoom is 48 ms and the maximum of two
post-load long tasks is 58 ms. Its 1,923 ms large-view load included one
five load-phase long tasks, with 296 ms as the maximum, which remain separately
reported as startup evidence.
The prior 141/181 measurements are superseded by this controlled run. The Ibex
read-only qualification is partial: physical collection is truncated and
mapping is incomplete.

Final C6 acceptance is 577 backend tests with one upstream Starlette/anyio
warning, 50 normal browser tests, and one isolated performance test. The
frontend lint and production build are clean. The controlled performance
evidence records 15,219,208-byte heap baseline, 39,289,888 bytes at 1,000
nodes, 24,070,680-byte growth, 1,343,444-byte cleanup residual, and unchanged
295,888/591,992-byte responses. Part 6's post-load interaction gate is closed;
the five load-phase long tasks remain a separate startup observation, with
296 ms as the maximum.

The matmul3 and known-core plans retain their own manifests and assurance
boundaries. Demo-PDK power, timing, and coarse Fmax are comparative evidence.
FPGA hardware-in-the-loop remains inconclusive until a board is available.

`ArchitectureContract`, `DesignSpec`, `RunManifest`, `EvidenceReport`, and
typed constraints are content-addressed records. Constraint and artifact
hashes are checked at subprocess boundaries. The SiliconCompiler runner uses
the `ASIC` and `Design` APIs and ends with a machine-readable `SCJOB {...}`
line; its output remains engineering evidence until an approved manufacturing
flow is used.

## Acceptance commands

```bash
cd backend && .venv/bin/python -m pytest -q
cd backend && .venv/bin/python -m netlist_qualification \
  --benchmark small --benchmark medium --benchmark large \
  --large-cells 100000 --node-limit 500
cd frontend && npm run lint && npm run build && npm run test:browser
cd frontend && npm run test:browser:perf
git diff --check
```

Use `scripts/run-matmul-qualification.py` for the frozen matmul contract and
`backend/qualify_existing_build.py` for read-only netlist/STA/layout collection
from an existing build. The latter launches no EDA or model process and writes
no artifact. Preserve exact reports, hashes, tool identities, and unavailable
states in the relevant plan.
