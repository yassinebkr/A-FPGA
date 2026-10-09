import asyncio
import websockets

async def test_agent():
    uri = "ws://localhost:8000/ws"
    async with websockets.connect(uri) as websocket:
        await websocket.send("I want to build a simple MAC unit")
        
        while True:
            try:
                response = await websocket.recv()
                print(f"Received: {response}")
                if "end" in response or "error" in response:
                    break
            except Exception as e:
                print(f"Exception: {e}")
                break

asyncio.run(test_agent())
