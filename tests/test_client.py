import json

import websockets
from kaji_agent.client import Agent


async def serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    return server, f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"


async def test_it_says_hello_with_its_token_and_acknowledges_a_task():
    seen = []

    async def gateway(ws):
        seen.append(ws.request.headers["Authorization"])
        hello = json.loads(await ws.recv())
        seen.append(hello["type"])
        await ws.send(json.dumps({"type": "welcome"}))
        await ws.send(json.dumps({"type": "task", "task": "t1", "payload": {"a": 1}}))
        seen.append(json.loads(await ws.recv()))
        await ws.close()

    server, url = await serve(gateway)
    agent = Agent(url, "kb_x")
    async with websockets.connect(url, additional_headers={"Authorization": "Bearer kb_x"}) as ws:
        await agent.session(ws)
    server.close()
    assert seen == ["Bearer kb_x", "hello", {"type": "ack", "task": "t1"}]
