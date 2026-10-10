# Agent reference — EDA and operations

## Docker deployment

The supported container stack builds the backend/EDA image from the pinned
SiliconCompiler runner image and serves the frontend through nginx. The image
uses a non-root `agent` user with uid/gid 1000. Compose publishes the backend
on `127.0.0.1:8001` and the frontend on `127.0.0.1:3000`; host development
uses port 8000. Bind mounts preserve `backend/projects`, `backend/logs`, and
`backend/data`; SC builds/cache use the configured volume.

```bash
docker compose up -d --build
curl http://127.0.0.1:8001/health
```

The image build is pinned by tag and digest; `Dockerfile`, `.dockerignore`,
`requirements.lock`, and the SC patch must remain aligned. Compose uses
`VITE_BACKEND_PORT=8001` in the production bundle and bind-mounts
`./backend/projects`, `./backend/logs`, and `./backend/data`. The SC data
volume is `${SC_DATA_HOST:-sc_data}:/data`, and `sc_cache` stores the SC cache.

Root `.env` is ignored and supplies provider keys to Compose. Keys are never
baked into images. The Dockerfile patch for negative OpenROAD utilization is
checked against the pinned SiliconCompiler release. The `sc_cache` volume must
be writable by `agent`; repair an existing volume with the documented root
compose command when required. `docker compose up -d` recreates containers
after environment changes; `docker compose restart` keeps the old environment.
On the development machine Docker's data root is configured through
`/etc/docker/daemon.json` at `/mnt/cold-hdd/docker`.

## Host workflow and EDA tiers

```bash
cd backend
./start.sh
cd ../frontend
npm install
npm run dev
```

`backend/start.sh` uses uvicorn reload with
`--timeout-graceful-shutdown 3` and a three-second graceful shutdown.
The frontend reconnects every two seconds; kill orphaned `uvicorn main:app`
processes if a reloader retains port 8000.

EDA storage comes from `SCPATH` and `SC_BUILDS`, which point to `/mnt/cold-hdd`
on the development machine. The backend detects tools per call and fails
closed when the requested tier is unavailable:

- Yosys plus Verilator/Icarus: lint, simulation, synthesis.
- OpenSTA (`sta`): constrained STA and bounded `find_fmax`.
- OpenROAD plus KLayout: full P&R and physical layout artifact.

The local source-built installers are `install-local-eda.sh`,
`install-local-timing-runtime.sh`, `install-local-formal.sh`, and
`install-klayout.sh`. The timing runtime belongs in `LD_LIBRARY_PATH`; editor
bundled C++ libraries can break Yosys/Icarus. OpenSTA is built under
`/mnt/cold-hdd/OpenSTA/build`; OpenROAD and KLayout use the user Nix profile.
The usual Nix package is `nixpkgs#openroad`; KLayout is installed by
`install-klayout.sh`. The formal local prefix includes `sby` and `Z3` when
the formal installer succeeds.

Clocked RTL receives an SDC with clock, uncertainty, I/O delays, load, and
`set_false_path` constraints for asynchronous reset. `find_fmax` remains a coarse bounded
synthesis/STA search with explicit uncertainty; it is an engineering estimate.
The generated constraints may be written as `<top>.sdc`; SiliconCompiler
manifests and reports live below `<build>/<top_module>/`, including
`job0/<top>.pkg.json`. Full-P&R outputs may include `<top>.odb.gz` and
`.def.gz`; the browser consumes bounded derived artifacts.

## Field fixes that must survive rebuilds

1. SiliconCompiler/OpenROAD can emit a negative utilization sentinel; the
   local `_apr.py` patch skips that metric until the upstream fix is present.
2. The installed `freepdk45_demo` flow has a broken antenna-repair dependency;
   the default is `skywater130_demo`.
3. The SC cache volume must inherit ownership by the non-root image user.

Reapply the local patch after rebuilding the virtual environment and remove it
after upgrading to a release containing the upstream fix.

## Tiny Tapeout and manufacturing boundary

The gitignored Tiny Tapeout workspace contains the `tt_um_counter8` wrapper,
its contract, testbench, and metadata. The local Sky130 hardening result is
recorded as a clean 80/80 LibreLane run. Cloud submission, provider review,
shuttle acceptance, payment, and shared-wafer release remain future manual
steps. A local hardening result is engineering evidence and carries no foundry
signoff claim. The upstream workspace is `TinyTapeout/ttsky-verilog-template`
and cloud automation uses `TinyTapeout/tt-gds-action`; the local equivalent is
`./tt/tt_tool.py --harden`.

## Local-only security boundary

The backend binds for local development and permits broad CORS. Authentication,
ownership, origin controls, and resource limits are required before network
deployment. Keep `backend/data`, logs, projects, `.env`, `.tools`, and
third-party tapeout state ignored.
