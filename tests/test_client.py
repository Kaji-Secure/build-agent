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
    assert (
        info["cluster_cpus"] == 3.92 + 8 + 2
    )  # all the nodes together: the agent's Jobs go to whichever node the scheduler picks


def test_a_tainted_or_cordoned_node_does_not_count_for_what_the_cluster_can_run():
    from kaji_agent.client import node_info

    plane = {
        **node("arm64", "16", "64Gi"),
        "spec": {"taints": [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]},
    }
    cordoned = {**node("amd64", "12", "32Gi"), "spec": {"unschedulable": True}}
    prefer = {**node("amd64", "4", "8Gi"), "spec": {"taints": [{"key": "x", "effect": "PreferNoSchedule"}]}}
    down = {**node("amd64", "12", "32Gi"), "status": {**node("amd64", "12", "32Gi")["status"], "conditions": [{"type": "Ready", "status": "False"}]}}
    up = {**node("amd64", "4", "8Gi")}
    up["status"] = {**up["status"], "conditions": [{"type": "MemoryPressure", "status": "False"}, {"type": "Ready", "status": "True"}]}
    info = node_info(Nodes([plane, cordoned, down, prefer, up]))
    assert (
        info["arches"] == ["amd64"]
        and info["cpus"] == 4
        and info["cluster_cpus"] == 8
        and info["memory"] == 8 * 1024**3
    )


def test_it_reports_only_the_host_when_it_may_not_list_nodes():
    from kaji_agent.client import node_info

    class Forbidden:
        def request(self, *a):
            raise OSError("403")

    assert "arches" not in node_info(Forbidden()) and "arch" in node_info(Forbidden())


async def test_an_agent_the_gateway_refuses_fails_and_says_why():
    import pytest

    async def gateway(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "refused", "reason": "20 of the 16 cores"}))
        await ws.close(4403)

    server, url = await serve(gateway)
    agent = Agent(url, "kb_x")
    with pytest.raises(SystemExit, match="refused this agent: 20 of the 16 cores"):
        async with websockets.connect(url, additional_headers={"Authorization": "Bearer kb_x"}) as ws:
            await agent.session(ws)
    server.close()


def test_the_hello_carries_the_threads_per_core_and_the_budget(monkeypatch):
    from kaji_agent import client

    monkeypatch.setenv("KAJI_CPUS", "12")
    info = client.node_info()
    assert info["cpu_budget"] == 12.0 and info["cpu_budget_from"] == "KAJI_CPUS" and info["threads_per_core"] >= 1


def test_the_hello_says_the_role():
    from kaji_agent.client import node_info

    assert node_info()["role"] == "builder" and node_info(role="host")["role"] == "host"


def test_the_agent_says_its_role_in_its_hello():
    import asyncio

    seen = []

    async def run():
        async def gateway(ws):
            seen.append(json.loads(await ws.recv())["info"]["role"])
            await ws.close()

        server, url = await serve(gateway)
        async with websockets.connect(url) as ws:
            await Agent(url, "kb_x", role="cluster").session(ws)
        server.close()

    asyncio.run(run())
    assert seen == ["cluster"]


def test_a_role_that_is_not_built_yet_stops_the_agent_with_a_message(monkeypatch, capsys):
    import pytest
    from kaji_agent import __main__ as entry

    monkeypatch.setattr("sys.argv", ["kaji-agent", "--token", "kb_x", "--role", "host"])
    with pytest.raises(SystemExit):
        entry.main()
    assert "the host role is not built yet" in capsys.readouterr().err
