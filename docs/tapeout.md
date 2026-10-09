# Tiny Tapeout Preparation Guide

This document describes how the autonomous agent (and you) can prepare a verified design for submission to [Tiny Tapeout](https://tinytapeout.com).

> **Scope:** This is a *preparation* step (Task 7). No shuttle seat is purchased here.
> Actual submission is a future manual step that requires a public GitHub repository and the Tiny Tapeout submission flow.

> **Current repository status:** A local Tiny Tapeout Sky130 hardening run for `tt_um_counter8` completed successfully on 2026-10-01 using LibreLane 3.0.14 (container digest `sha256:f91b21d75f79871f9ccf37451020b5d2f7b3236881a997ff709c561e8280a30f`) and Sky130 PDK revision `8afc8346a57fe1ab7934ba5a6056ea8b43078e71`. All 80/80 stages completed with flow exit 0; Magic/KLayout DRC, LVS, antenna, and setup/hold checks were reported clean, including `klayout__drc_error__count=0`. LibreLane consumed the explicit `src/pnr.sdc` and `src/signoff.sdc` constraints, with no missing-SDC fallback warnings. This is open-source demo/Tiny Tapeout hardening evidence; foundry signoff and provider acceptance remain with the selected provider. The optional LibreLane post-layout Yosys.EQY path was not accepted because its boxed-cell combine step is incompatible with this generated TT wrapper/netlist; no post-layout equivalence is claimed. Independent project RTL equivalence remains the required gate.

## What “ready” means

This guide prepares a **manufacturing candidate**: a design that has passed the project’s configured verification and implementation checks, has a reproducible wrapper and test, and is small and slow enough for the selected Tiny Tapeout profile. It does not constitute commercial/foundry signoff. Signoff is the final release decision based on the selected shuttle’s approved PDK, rule decks, libraries, constraints, and required checks (for example DRC/LVS/ERC, antenna, timing, power, and reliability checks).

Before purchasing a shared-wafer or Tiny Tapeout seat, confirm the current shuttle documentation for the exact process and determine which checks the provider runs. Keep the project’s open-source evidence, tool versions, logs, waivers, and unresolved limitations with the submission. A clean local run is evidence for review; it is not permission to claim that the design is silicon-safe without the provider/foundry flow and engineering review.

## Supported shuttles

Tiny Tapeout currently offers three process flavors:

| Shuttle | Process      | Template repository used here                |
|---------|--------------|----------------------------------------------|
| TTSKY   | SkyWater 130 | `TinyTapeout/ttsky-verilog-template`         |
| TTIHP   | IHP SG13G2   | `TinyTapeout/ttihp-verilog-template`         |
| TTGF    | GF180MCU     | `TinyTapeout/ttgf-verilog-template`          |

This project uses the **Sky130 (`ttsky`)** template as the default. The template is cloned into:

```text
tapeout/ttsky-verilog-template/
```

The `tapeout/` directory is gitignored so the upstream template is not committed as project code.

## Agent convention: "make this ready for Tiny Tapeout"

When the user asks to prepare a design for tapeout, the agent should:

1. Take the **verified design** (the design that already passes simulation).
2. Wrap it in a top-level module named `tt_um_<design>`.
3. Place the wrapper file in `tapeout/ttsky-verilog-template/src/tt_um_<design>.v`.
4. Update `tapeout/ttsky-verilog-template/info.yaml`:
   - `project.title`
   - `project.author`
   - `project.description`
   - `project.clock_hz`
   - `project.top_module` → `tt_um_<design>`
   - `project.source_files` → list the wrapper source(s)
5. Update `tapeout/ttsky-verilog-template/test/Makefile`:
   - `PROJECT_SOURCES` → the wrapper source(s)
6. Update `tapeout/ttsky-verilog-template/test/tb.v` to instantiate `tt_um_<design>`.
7. Add or adapt a self-checking cocotb test in `tapeout/ttsky-verilog-template/test/test.py`.
8. Keep the design **≤ 1–2 k gates** for a 1×1 tile (the default). Use a 2×2 tile only if the design is genuinely larger.
9. Target the **40–66 MHz** low-risk clock band. The default `src/config.json` uses a 20 ns (50 MHz) clock period, which is a safe starting point.

## Tiny Tapeout wrapper contract

The top module **must** be named `tt_um_<name>` and expose exactly the following ports:

```verilog
module tt_um_<name> (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // Bidirectional: input path
    output wire [7:0] uio_out,  // Bidirectional: output path
    output wire [7:0] uio_oe,   // Bidirectional: output enable (1 = output)
    input  wire       ena,      // Harness enable (tied high; may be ignored)
    input  wire       clk,      // External clock
    input  wire       rst_n     // Active-low reset
);
```

All dedicated outputs and bidirectional enable pins must be assigned. Unused bidirectional pins should be set to input mode (`uio_oe = 0`) and their output values driven to `0`.

## IO budget for the example counter

The included `tt_um_counter8` example maps the IOs as follows:

| Pin(s)   | Direction | Function                              |
|----------|-----------|---------------------------------------|
| `ui_in[0]` | input   | Count enable (`1` = count, `0` = hold) |
| `ui_in[7:1]` | input | Unused (tied off)                      |
| `uo_out[7:0]` | output | 8-bit counter value                   |
| `uio_*`  | input     | Bidirectional pins unused (all inputs) |
| `clk`    | input     | External clock                         |
| `rst_n`  | input     | Active-low reset                       |
| `ena`    | input     | Harness enable (ignored)               |

## Clock guidance

- The RP2040 on the Tiny Tapeout demo board can generate **1 Hz – 66.5 MHz**.
- Tiny Tapeout guarantees the pad path works for **≥ 50 MHz**.
- The safe operating band for most 1×1 designs is **40–66 MHz**.
- The default `CLOCK_PERIOD` in `src/config.json` is **20 ns = 50 MHz**. Increase the period (lower the frequency) if setup violations occur; decrease it only if timing is clean and you need the extra speed.

## Tile sizing

| Tile size | Approximate gate budget | Use case                          |
|-----------|--------------------------|-----------------------------------|
| 1×1       | 1–2 k gates              | Default for small designs         |
| 2×2       | 4–8 k gates              | Larger designs only               |

Keep the first tapeout attempt at **1×1** unless the design clearly does not fit.

## Local hardening run and evidence

Local hardening runs the same LibreLane flow that the cloud GDS action uses. It requires:

- Docker
- The Sky130 PDK (`PDK_ROOT` / `PDK` environment variables)
- `TinyTapeout/tt-support-tools` cloned into `tt/`
- `librelane==3.0.14`

Typical one-time setup:

```bash
cd tapeout/ttsky-verilog-template
git clone https://github.com/TinyTapeout/tt-support-tools tt
git -C tt checkout 01d5d2814fa9dd61e9d211e0b235a4a592a9316
# install librelane==3.0.14 in the Python environment you use for tt_tool.py
export PDK_ROOT=/path/to/pdk
export PDK=sky130A
```

Run the local hardening:

```bash
cd tapeout/ttsky-verilog-template
./tt/tt_tool.py --harden
```

> The local run completed on 2026-10-01 for `tt_um_counter8`: 80/80 stages, flow exit 0, Magic/KLayout DRC, LVS, antenna, and setup/hold checks reported clean, with `klayout__drc_error__count=0`. Record the LibreLane 3.0.14 container digest `sha256:f91b21d75f79871f9ccf37451020b5d2f7b3236881a997ff709c561e8280a30f` and Sky130 PDK revision `8afc8346a57fe1ab7934ba5a6056ea8b43078e71` with the regenerated `tt_submission/` package. LibreLane consumed `src/pnr.sdc` and `src/signoff.sdc` directly, so this run has no missing-SDC fallback warning. This evidence covers open-source demo/Tiny Tapeout hardening; foundry signoff lies outside its scope.

The optional LibreLane post-layout `Yosys.EQY` path was attempted separately but failed at the boxed-cell combine step because that operation is incompatible with the generated Tiny Tapeout wrapper/netlist. This is not a claim of post-layout equivalence. The project's independent RTL equivalence result remains the required equivalence gate. Do not treat the local hardening result as commercial/foundry signoff; the selected provider must still accept the submission.
Preserve the hardening run's resolved configuration, command line, logs, and generated artifacts. A failed or incomplete local run is useful evidence but does not qualify the design for submission. The project’s ordinary synthesis+STA and verification evidence also remains distinct from Tiny Tapeout hardening evidence.

## Cloud submission

When the design is ready:

1. Push the `ttsky-verilog-template` contents to a public GitHub repository.
2. The `TinyTapeout/tt-gds-action@ttsky26d` GitHub Action will run LibreLane in the cloud on every push and produce GDS/LEF artifacts.
3. Submit the repository at <https://app.tinytapeout.com> and follow the checkout/payment flow for the desired shuttle.

Submission is still subject to the provider’s current acceptance and hardening results. If cloud hardening rejects the design, treat that as a release-blocking implementation failure and preserve the failure artifacts for diagnosis; do not convert a local “pass” into a signoff claim.

## Example layout

```text
tapeout/ttsky-verilog-template/
├── info.yaml                 # project metadata, top_module, source_files
├── src/
│   ├── config.json           # LibreLane configuration (keep defaults unless you know why)
│   └── tt_um_counter8.v      # wrapper + user design
└── test/
    ├── Makefile              # PROJECT_SOURCES must match info.yaml
    ├── tb.v                  # instantiates tt_um_counter8
    └── test.py               # self-checking cocotb tests
```

## Next manual steps

1. Preserve the completed hardening logs, manifests, container digest, PDK revision, configuration, and generated artifacts.
2. Preserve and review the optional post-layout EQY failure artifact, while requiring the independent project RTL-equivalence gate; no post-layout equivalence claim is made.
3. Re-run the documented flow when the template, tool image, PDK, or constraints change; capture the full logs and output revisions.
4. Decide on a GitHub repository name and push the template contents only after the wrapper, test, metadata, and hardening evidence are reviewed.
5. Submit the repository at <https://app.tinytapeout.com> when a shuttle is open, subject to the current provider checklist and acceptance result.

## Backend hardening and release-evidence API

The backend exposes the same conservative flow to the web UI, automation, and the agent tool loop:

- `GET /api/tapeout/capabilities` probes the local template, support tool, Docker/LibreLane, and PDK prerequisites without running a flow.
- `POST /api/tapeout/runs` accepts `wrapper_verilog`, `top_module`, metadata, and `clock_hz`. It runs through `JobManager`, streams the support tool output over the ordinary WebSocket job subscription, and returns the content-addressed input inventory plus a `job_id`. Missing prerequisites are `inconclusive`; they are never converted to a pass.
- Successful terminal hardening runs are automatically persisted as immutable canonical JSON under `backend/data/evidence/tapeout-runs/`. `GET /api/tapeout/runs` (alias `/api/tapeout/evidence`) lists valid records; `GET /api/tapeout/runs/{run_id}` (alias `/api/tapeout/evidence/{run_id}`) returns the validated record and its canonical SHA-256 digest. Corrupt, malformed, unsafe, or symlinked records fail closed.
- `POST /api/tapeout/release-gate` evaluates an explicit `TinyTapeoutRun` record from `backend/tapeout_repro.py` (and an optional rerun) against the mandatory DRC/LVS/timing and reproducibility requirements. The run's `assurance` declaration defaults to `local_dry_run` for legacy or omitted records; container presence alone never elevates it. The response includes reasons, evidence digest, rerun equivalence, and assurance. It does not claim foundry signoff.

The agent has the corresponding `run_tiny_tapeout_hardening` tool. Tool-controlled paths and tool configuration remain server-owned; the model can provide only wrapper source and project metadata. Hardening subprocesses run in their own process group, so job cancellation terminates the full support-tool tree and preserves the streamed log.

## Future engineer terminal interface

The web UI is the current operator interface. A future CLI/TUI should expose the same backend contracts for engineers working from a shell, CI runner, or remote development host. It is planned as a client over the existing REST and WebSocket APIs, keeping the backend as the single execution path.

The intended expert workflow is:

```text
select project/profile
  -> submit or resume verification/synthesis/Fmax jobs
  -> follow structured live logs and job status
  -> inspect constraints, timing/PPA, assurance level, and limitations
  -> export the evidence manifest and artifact hashes
```

The first version should support non-interactive JSON/text output for automation and an interactive terminal view for live jobs. It should make the distinction between local engineering evidence, Tiny Tapeout hardening evidence, and commercial/foundry signoff visible at every report boundary. No terminal client is implemented yet; this section records the architecture and intended future work.
