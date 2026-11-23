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


class Nodes:
    def __init__(self, items):
        self.items = items

    def request(self, method, path):
        assert (method, path) == ("GET", "/api/v1/nodes")
        return {"items": self.items}


def node(arch, cpu, memory):
    return {"status": {"nodeInfo": {"architecture": arch}, "allocatable": {"cpu": cpu, "memory": memory}}}


def test_it_reports_the_architectures_and_the_biggest_node_of_its_cluster():
    from kaji_agent.client import node_info

    info = node_info(
        Nodes([node("amd64", "3920m", "16Gi"), node("arm64", "8", "8388608Ki"), node("amd64", "2", "4Gi")])
    )
    assert info["arches"] == ["amd64", "arm64"] and info["cpus"] == 8 and info["memory"] == 16 * 1024**3


def test_it_reports_only_the_host_when_it_may_not_list_nodes():
    from kaji_agent.client import node_info

    class Forbidden:
        def request(self, *a):
            raise OSError("403")

    assert "arches" not in node_info(Forbidden()) and "arch" in node_info(Forbidden())
