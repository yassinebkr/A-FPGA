#!/usr/bin/env python3
"""One minimal paid end-to-end provider smoke through REST + WebSocket."""
from __future__ import annotations

import asyncio
import json
import urllib.request

import websockets


BASE = "http://127.0.0.1:8000"
MODEL = "kimi-k3"
PROMPT = "Do not call any tools. Reply with exactly: SMOKE OK"


def request_json(url: str, payload: dict | None = None):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


async def main() -> int:
    models = request_json(f"{BASE}/api/models")
    selected = next((item for item in models if item.get("key") == MODEL), None)
    if not selected or selected.get("health") != "ok":
        raise RuntimeError(f"{MODEL} is not healthy")
    project = request_json(f"{BASE}/api/projects", {
        "name": "paid-provider-smoke", "model": MODEL})
    frames = []
    async with websockets.connect("ws://127.0.0.1:8000/ws",
                                  max_size=10 * 1024 * 1024) as socket:
        await socket.send(json.dumps({"project": project["id"]}))
        while True:
            frame = json.loads(await asyncio.wait_for(socket.recv(), 30))
            frames.append(frame)
            if frame.get("type") == "history":
                break
        await socket.send(PROMPT)
        while True:
            frame = json.loads(await asyncio.wait_for(socket.recv(), 300))
            frames.append(frame)
            if frame.get("type") in {"end", "error"}:
                break
    final_text = "".join(str(frame.get("content") or "") for frame in frames
                         if frame.get("type") == "text").strip()
    streamed_text = "".join(str(frame.get("content") or "") for frame in frames
                            if frame.get("type") == "text_delta").strip()
    text = final_text or streamed_text
    tools = [frame for frame in frames if frame.get("type") == "tool_call"]
    errors = [frame for frame in frames if frame.get("type") == "error"]
    usage = [frame for frame in frames if frame.get("type") == "generation_usage"]
    result = {"project": project["id"], "model": MODEL, "text": text,
              "tool_calls": len(tools), "errors": len(errors),
              "usage": usage[-1] if usage else None,
              "passed": text == "SMOKE OK" and not tools and not errors}
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
