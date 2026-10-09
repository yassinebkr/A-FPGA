#!/usr/bin/env python3
"""Run a real SiliconCompiler Fmax acceptance and fail closed."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.prefix).resolve() != (BACKEND / ".venv").resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(BACKEND))

local_bin = ROOT / ".tools" / "eda" / "bin"
if local_bin.is_dir():
    os.environ["PATH"] = f"{local_bin}{os.pathsep}{os.environ.get('PATH', '')}"

local_lib = ROOT / ".tools" / "eda" / "lib"
if local_lib.is_dir():
    os.environ["LD_LIBRARY_PATH"] = f"{local_lib}{os.pathsep}{os.environ.get('LD_LIBRARY_PATH', '')}"
    os.environ["TCL_LIBRARY"] = str(local_lib / "tcl8.6")

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
if load_dotenv:
    load_dotenv(ROOT / ".env")
    load_dotenv(BACKEND / ".env")

import eda_tools


RTL = """module fmax_acceptance (
    input wire clk,
    input wire rst_n,
    input wire en,
    output reg [31:0] q
);
always @(posedge clk or negedge rst_n) begin
    if (!rst_n)
        q <= 32'b0;
    else if (en)
        q <= q + 32'd1;
end
endmodule
"""


async def run(args: argparse.Namespace) -> int:
    try:
        text = await eda_tools.find_fmax(
            RTL,
            "fmax_acceptance",
            target=args.target,
            period_tolerance_ns=args.tolerance,
            max_runs=args.max_runs,
            confirm_runs=args.confirm_runs,
            min_period_ns=args.min_period,
            max_period_ns=args.max_period,
        )
        print(text)
        payload = json.loads(text.splitlines()[0])
        search = payload.get("search", {})
        passed = (
            payload.get("status") == "success"
            and search.get("confirmation", {}).get("status") == "stable"
            and search.get("monotonicity", {}).get("status") == "valid"
            and search.get("uncertainty_ns", float("inf")) <= args.tolerance
        )
        print("FMAX_ACCEPTANCE=PASS" if passed else "FMAX_ACCEPTANCE=FAIL")
        return 0 if passed else 1
    finally:
        await eda_tools.jobs_mod.manager.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="skywater130_demo")
    parser.add_argument("--tolerance", type=float, default=0.5)
    parser.add_argument("--max-runs", type=int, default=12)
    parser.add_argument("--confirm-runs", type=int, default=1)
    parser.add_argument("--min-period", type=float, default=5.0)
    parser.add_argument("--max-period", type=float, default=40.0)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
