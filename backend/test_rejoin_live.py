"""Manual probe: mid-run rejoin — client A starts a chat and disconnects,
client B joins the same project and must see busy + missed events + live tail."""
import asyncio, json, urllib.request
import websockets

BASE = "http://127.0.0.1:8001"
WS = "ws://127.0.0.1:8001/ws"
PROMPT = ("Write a blinky Verilog module (top: blinky), lint it, simulate a quick "
          "testbench, then tell me the result. Keep it small.")

def create_project():
    req = urllib.request.Request(f"{BASE}/api/projects",
        data=json.dumps({"name": "rejoin-probe"}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)["id"]

async def client_a(pid):
    async with websockets.connect(WS, max_size=50*1024*1024) as ws:
        await ws.send(json.dumps({"project": pid}))
        await ws.recv()  # history
        await ws.send(PROMPT)
        seen = []
        while len(seen) < 2:  # grab the first 2 chat events then vanish
            msg = json.loads(await asyncio.wait_for(ws.recv(), 120))
            if msg.get("type") in ("text", "tool_call", "tool_result"):
                seen.append(msg["type"])
        print("A saw:", seen, "-> disconnecting mid-run")

async def client_b(pid):
    got = []
    async with websockets.connect(WS, max_size=50*1024*1024) as ws:
        await ws.send(json.dumps({"project": pid}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 600))
            got.append(msg.get("type"))
            if msg.get("type") in ("end", "error"):
                break
    return got

async def main():
    pid = create_project()
    print("project:", pid)
    await client_a(pid)
    await asyncio.sleep(1)
    got = await client_b(pid)
    print("B frames:", got[:10], "..." if len(got) > 10 else "")
    assert "history" in got, "no history frame"
    assert "busy" in got, "NO busy frame — rejoin did not signal in-flight run"
    assert got.index("busy") < got.index("end"), "busy must precede end"
    n_tool = sum(1 for t in got if t in ("tool_call", "tool_result"))
    print(f"PASS: busy frame present, {n_tool} tool events streamed, run completed live")
    req = urllib.request.Request(f"{BASE}/api/projects/{pid}", method="DELETE")
    urllib.request.urlopen(req)
    print("probe project deleted")

asyncio.run(main())
