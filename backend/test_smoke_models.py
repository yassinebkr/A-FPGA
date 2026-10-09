"""Manual smoke probe: same trivial prompt through two registry models (needs live server)."""
import asyncio, json, sys
import urllib.request
import websockets

BASE = "http://127.0.0.1:8001"
PROMPT = "Do not call any tools. Reply with exactly: SMOKE OK"

def create_project(name, model):
    req = urllib.request.Request(
        f"{BASE}/api/projects",
        data=json.dumps({"name": name, "model": model}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)["id"]

async def chat_once(project_id):
    frames = []
    async with websockets.connect("ws://127.0.0.1:8001/ws", max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"project": project_id}))
        # drain history frame(s) briefly
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 2))
                if msg.get("type") == "history":
                    break
        except asyncio.TimeoutError:
            pass
        await ws.send(PROMPT)
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 120))
            frames.append(msg)
            if msg.get("type") == "end":
                break
            if msg.get("type") == "error":
                break
    return frames

async def main():
    for model in ("kimi-k3", "openrouter-kimi-k3"):
        pid = create_project(f"smoke-{model}", model)
        frames = await chat_once(pid)
        text = "".join(f.get("content", "") for f in frames if f.get("type") == "text")
        tools = [f.get("content") for f in frames if f.get("type") == "tool_call"]
        errs = [f.get("content") for f in frames if f.get("type") == "error"]
        print(f"--- {model} (project {pid})")
        print(f"    text: {text.strip()[:200]!r}")
        print(f"    tool_calls: {tools}")
        print(f"    errors: {errs}")

asyncio.run(main())
