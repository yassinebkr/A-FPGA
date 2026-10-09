#!/usr/bin/env python3
"""Run one explicitly bounded matmul3 repair through the public API/WS path."""
from __future__ import annotations

import argparse
import asyncio
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import websockets


PROMPT = """Implement the frozen matmul3 qualification benchmark exactly. Start from a fresh design, not prior chat prose. The independently verified baseline misses the 20 ns Sky130 screen at setup slack -3.03 ns / Fmax 43.43 MHz. Preserve the exact frozen interface, signed arithmetic, latency=10 cycles and II=10. Use a timing architecture that isolates multipliers and bounds every adder depth. Run lint, the self-checking frozen-contract simulation, and at most two synthesis+STA screens at 20 ns. Stop deterministically if the second screen misses. Do not run full P&R or find_fmax. Preserve every RTL/testbench revision as durable content-addressed artifacts and report measured evidence only."""


def request_json(url: str, *, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def frozen_contract(frame: dict) -> bool:
    spec = ((frame.get("contract") or {}).get("spec") or {})
    timing = spec.get("timing_goals") or {}
    metadata = spec.get("metadata") or {}
    arch = metadata.get("architecture_contract") or {}
    return (
        spec.get("top_module") == "systolic_mm_4x4"
        and spec.get("target") == "sky130"
        and float(timing.get("clock_target_mhz", arch.get("clock_target_mhz", 0))) == 50.0
        and float(timing.get("clock_period_ns", arch.get("clock_period_ns", 0))) == 20.0
        and int(timing.get("latency_cycles", arch.get("latency_cycles", -1))) == 10
        and abs(float(timing.get("throughput_per_cycle", arch.get("throughput_per_cycle", 0))) - 0.1) < 1e-9
    )


async def run(base: str, output: Path) -> int:
    models = request_json(f"{base}/api/models")
    entries = models.get("models", models) if isinstance(models, dict) else models
    available = [item for item in entries
                 if item.get("available") and item.get("health") == "ok"]
    # This qualification run intentionally prefers the direct Kimi provider;
    # the chosen registry key is persisted on the new project and therefore
    # remains the authoritative provider throughout the run.
    preferred = next((item for item in available if item["key"] == "kimi-k3"), None)
    selected = preferred or next((item for item in available if item.get("default")), None) or (available[0] if available else None)
    if not selected:
        raise RuntimeError("no configured model provider has health=ok")
    project = request_json(f"{base}/api/projects", payload={
        "name": "matmul3-bounded-repair", "model": selected["key"]})
    project_id = project["id"]
    frames: list[dict] = []
    uri = base.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    async with websockets.connect(uri, max_size=50 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"project": project_id}))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), 30))
            frames.append(frame)
            if frame.get("type") == "history":
                break
        await ws.send(PROMPT)
        proposal = None
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), 1200))
            frames.append(frame)
            if frame.get("type") == "contract_state" and frame.get("status") in {"ready", "draft"}:
                proposal = frame
            if frame.get("type") == "end":
                break
        if not proposal or not frozen_contract(proposal):
            raise RuntimeError("agent did not produce the exact frozen matmul3 contract")
        await ws.send(json.dumps({
            "type": "contract_action", "project": project_id,
            "action": "contract.approve_and_build",
            "expected_revision": proposal["revision"],
            "expected_digest": proposal["digest"],
        }))
        saw_locked = False
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), 1800))
            frames.append(frame)
            if frame.get("type") == "contract_state" and frame.get("status") == "locked":
                saw_locked = True
            if saw_locked and frame.get("type") == "end":
                break
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output / f"{project_id}-{stamp}.json"
    path.write_text(json.dumps({"project": project_id, "model": selected["key"],
        "prompt": PROMPT, "frames": frames}, indent=2) + "\n")
    print(json.dumps({"project": project_id, "model": selected["key"],
        "frames": len(frames), "transcript": str(path)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path,
        default=Path(__file__).resolve().parents[1] / "backend/data/qualifications/matmul3/autonomous")
    args = parser.parse_args()
    return asyncio.run(run(args.base, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
