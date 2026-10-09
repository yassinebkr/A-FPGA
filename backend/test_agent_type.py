import asyncio
from agent_core import fpga_agent

async def test():
    async with fpga_agent as agent:
        response = await agent.chat("say hello")
        print(f"type: {type(response)}")
        print(f"dir: {dir(response)}")
        if hasattr(response, 'text'):
            print(f"text type: {type(response.text)}")
            if callable(response.text):
                print(f"text(): {response.text()}")

asyncio.run(test())
