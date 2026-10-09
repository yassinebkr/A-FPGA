from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            print(f"Received: {data}")
            await asyncio.sleep(1) # simulate thinking
            await websocket.send_json({"type": "tool_call", "content": "run_silicon_compiler"})
            await asyncio.sleep(1) # simulate working
            await websocket.send_json({"type": "tool_result", "content": "Synthesis successful!"})
            await websocket.send_json({"type": "text", "content": f"Mock response to: {data}"})
            await websocket.send_json({"type": "end"})
    except WebSocketDisconnect:
        pass

@app.get("/health")
def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
