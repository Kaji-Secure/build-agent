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

    monkeypatch.setattr(
        "sys.argv", ["kaji-agent", "--token", "kb_x", "--role", "cluster"]
    )
    with pytest.raises(SystemExit):
        entry.main()
    assert "the cluster role is not built yet" in capsys.readouterr().err


def test_a_host_agent_needs_no_cluster_and_a_report_interval_under_a_minute_is_refused(
    monkeypatch, capsys
):
    import pytest
    from kaji_agent import __main__ as entry

    monkeypatch.setattr(
        "sys.argv",
        ["kaji-agent", "--token", "kb_x", "--role", "host", "--interval", "5"],
    )
    with pytest.raises(SystemExit):
        entry.main()
    assert "at least 60 seconds" in capsys.readouterr().err


def test_a_node_the_user_switched_off_is_not_counted_in_the_clusters_capacity():
    from kaji_agent.client import node_info

    off = {**node("amd64", "16", "64Gi"), "metadata": {"labels": {"kaji": "NoBuilder"}}}
    info = node_info(Nodes([node("amd64", "4", "8Gi"), off]))
    assert info["cpus"] == 4 and info["cluster_cpus"] == 4


async def test_a_task_kaji_drops_has_its_job_removed():
    class Kube:
        namespace = "kaji"

        def __init__(self):
            self.calls = []

        def request(self, method, path, body=None, raw=False):
            self.calls.append((method, path))
            return {"items": []}

    kube = Kube()

    async def gateway(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "cancel", "task": "0b7a3c9e-1111-2222-3333-444455556666"}))
        await asyncio.sleep(0.2)
        await ws.close()

    import asyncio

    server, url = await serve(gateway)
    async with websockets.connect(url, additional_headers={"Authorization": "Bearer kb_x"}) as ws:
        await Agent(url, "kb_x", kube=kube).session(ws)
    server.close()
    assert any(m == "DELETE" and "/jobs/" in p and "propagationPolicy=Background" in p for m, p in kube.calls)


# ---------------------------------------------------------------- the version, and a Job that cannot be removed

def test_the_hello_carries_the_agents_version(monkeypatch):
    from kaji_agent import client

    monkeypatch.setenv("KAJI_AGENT_VERSION", "0.0.4")
    assert client.node_info()["version"] == "0.0.4"


def test_a_checkout_that_was_not_stamped_says_unknown(monkeypatch):
    from kaji_agent import version

    monkeypatch.delenv("KAJI_AGENT_VERSION", raising=False)
    monkeypatch.setattr(version, "STAMPED", "")
    assert version.agent_version() == "unknown"
    monkeypatch.setattr(version, "STAMPED", "1.2.3")
    assert version.agent_version() == "1.2.3"  # what the release build wrote
    monkeypatch.setenv("KAJI_AGENT_VERSION", "9.9.9")
    assert version.agent_version() == "9.9.9"  # an override wins


async def test_a_job_that_cannot_be_removed_is_logged_and_the_connection_goes_on(caplog):
    import asyncio

    from kaji_agent.kube import KubeError

    class Kube:
        namespace = "kaji"

        def request(self, method, path, body=None, raw=False):
            if method == "DELETE":
                raise KubeError(403, "jobs.batch is forbidden: cannot delete")
            return {"items": []}

    seen = []

    async def gateway(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "cancel", "task": "0b7a3c9e-1111-2222-3333-444455556666"}))
        await ws.send(json.dumps({"type": "task", "task": "t2", "payload": {}}))
        seen.append(json.loads(await ws.recv()))  # still answering: the refused delete did not end the session
        await ws.close()

    server, url = await serve(gateway)
    with caplog.at_level("WARNING", logger="kaji_agent"):
        async with websockets.connect(url, additional_headers={"Authorization": "Bearer kb_x"}) as ws:
            await Agent(url, "kb_x", kube=Kube()).session(ws)
    server.close()
    assert seen == [{"type": "ack", "task": "t2"}] and "could not remove the Job" in caplog.text and "forbidden" in caplog.text


# ---------------------------------------------------------------- enrolling a host


async def test_a_host_enrolls_with_its_token_and_hostname_and_gets_its_own():
    from kaji_agent import enroll

    seen = []

    async def gateway(ws):
        seen.append(ws.request.headers["Authorization"])
        seen.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "enrolled", "name": "edge-7", "token": "kb_mine"}))

    server, url = await serve(gateway)
    assert await enroll.enroll(url, "ke_fleet", "web1", name="edge-7") == ("edge-7", "kb_mine")
    server.close()
    assert seen == ["Bearer ke_fleet", {"type": "enroll", "hostname": "web1", "name": "edge-7"}]


async def test_the_gateways_reason_for_refusing_to_enroll_is_the_message():
    import pytest
    from kaji_agent import enroll

    async def gateway(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "refused", "reason": "Acme has reached the Free limit of 1 host"}))

    server, url = await serve(gateway)
    with pytest.raises(SystemExit, match="Free limit of 1 host"):
        await enroll.enroll(url, "ke_fleet", "web1")
    server.close()


async def test_an_answer_that_is_not_an_enrollment_is_not_taken_for_one():
    import pytest
    from kaji_agent import enroll

    async def gateway(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "enrolled", "token": "ke_not_an_agent_token"}))

    server, url = await serve(gateway)
    with pytest.raises(SystemExit):
        await enroll.enroll(url, "ke_fleet", "web1")
    server.close()


def test_the_token_is_kept_for_the_agent_only_and_read_back(tmp_path):
    import stat

    from kaji_agent import enroll

    assert enroll.saved_token(tmp_path / "state") == ""
    enroll.save_token(tmp_path / "state", "kb_mine")
    assert enroll.saved_token(tmp_path / "state") == "kb_mine"
    assert stat.S_IMODE((tmp_path / "state" / "token").stat().st_mode) == 0o600
    (tmp_path / "state" / "token").write_text("ke_an_enrollment_token")
    assert enroll.saved_token(tmp_path / "state") == ""  # only an agent's own token counts


def test_a_host_enrolls_once_then_starts_with_the_token_it_kept(monkeypatch, tmp_path):
    from kaji_agent import __main__ as entry

    calls, started = [], []

    async def fake_enroll(url, token, hostname=None, name=""):
        calls.append((token, name))
        return "web1", "kb_mine"

    class Stop(Exception):
        pass

    class FakeAgent:
        def __init__(self, url, token, kube, role, interval):
            started.append(token)

        async def run(self):
            raise Stop

    monkeypatch.setattr("kaji_agent.enroll.enroll", fake_enroll)
    monkeypatch.setattr(entry, "Agent", FakeAgent)
    monkeypatch.setenv("KAJI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("KAJI_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["kaji-agent", "--role", "host", "--enroll-token", "ke_fleet", "--name", "edge"])
    for _ in range(2):  # the second start: the token is there, the enrollment token is not used again
        try:
            entry.main()
        except Stop:
            pass
    assert calls == [("ke_fleet", "edge")] and started == ["kb_mine", "kb_mine"]
    assert (tmp_path / "token").read_text().strip() == "kb_mine"


def test_an_enrollment_token_is_for_host_agents_only(monkeypatch, capsys, tmp_path):
    import pytest
    from kaji_agent import __main__ as entry

    monkeypatch.setenv("KAJI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("KAJI_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["kaji-agent", "--role", "builder", "--enroll-token", "ke_fleet"])
    with pytest.raises(SystemExit):
        entry.main()
    assert "for host agents" in capsys.readouterr().err
