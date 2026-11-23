"""The CPU of each node of the cluster, read by a short Job on it (nodeprobe.py): a Node object does not say."""
import asyncio
import json

import websockets
from kaji_agent import nodeprobe, runner, sysinfo
from kaji_agent.client import Agent

NS = "kaji-agent"
X86 = "processor\t: 0\nvendor_id\t: GenuineIntel\nmodel name\t: AMD EPYC 7763 64-Core   Processor\n"


class Kube:
    """A cluster: its own pod, and Jobs that finish with a log when asked."""

    namespace = NS

    def __init__(self, logs, pull=None, fail_create=(), never=()):
        self.logs, self.pull, self.fail_create, self.never = logs, pull or [], set(fail_create), set(never)
        self.jobs, self.calls = {}, []

    def request(self, method, path, body=None, raw=False):
        self.calls.append((method, path))
        if method == "GET" and path.endswith("/pods/agent-pod-1"):
            return {"spec": {"containers": [{"image": "ghcr.io/kaji-secure/kaji-agent:0.0.4"}], "imagePullSecrets": self.pull}}
        if method == "POST":
            node = body["spec"]["template"]["spec"]["nodeName"]
            if node in self.fail_create:
                raise OSError("forbidden")
            self.jobs[body["metadata"]["name"]] = body
            return body
        if method == "DELETE":
            return self.jobs.pop(path.split("/jobs/")[1].split("?")[0], None)
        if "/log" in path:
            return self.logs[self._node_of_pod(path)]
        if "/pods?" in path:
            name = path.split("job-name%3D")[1]
            return {"items": [{"metadata": {"name": "pod-of-" + name}}]}
        name = path.split("/jobs/")[1]
        job = self.jobs.get(name)
        if job is None:
            return None
        node = job["spec"]["template"]["spec"]["nodeName"]
        return {"status": {} if node in self.never else {"succeeded": 1}}

    def _node_of_pod(self, path):
        name = path.split("/pods/pod-of-")[1].split("/")[0]
        return next(j["spec"]["template"]["spec"]["nodeName"] for n, j in self.created.items() if n == name)

    @property
    def created(self):
        return {**self.jobs, **getattr(self, "_gone", {})}


def probe(kube, nodes, monkeypatch, **kw):
    monkeypatch.setattr(nodeprobe.platform, "node", lambda: "agent-pod-1")
    gone = {}
    original = kube.request

    def keep(method, path, body=None, raw=False):  # a deleted Job's pod log is still read before: remember what was made
        if method == "POST":
            gone[body["metadata"]["name"]] = body
        kube._gone = gone
        return original(method, path, body, raw)

    kube.request = keep
    return nodeprobe.cpu_models(kube, nodes, sleep=lambda s: None, **kw)


def test_the_cpu_is_read_from_the_log_and_tidied():
    assert nodeprobe.parse(X86)["model"] == "AMD EPYC 7763 64-Core Processor"
    assert nodeprobe.parse("Hardware\t: Rockchip RK3588\n")["model"] == "Rockchip RK3588"
    assert nodeprobe.parse("") == {} and nodeprobe.parse("processor\t: 0\n") == {}  # no model, nothing to show


def test_each_node_gets_a_job_on_it_that_reads_its_cpu_and_is_then_removed(monkeypatch):
    kube = Kube({"n1": X86, "n2": "model name\t: Intel Xeon\n"}, pull=[{"name": "regcred"}])
    assert {n: i["model"] for n, i in probe(kube, ["n1", "n2"], monkeypatch).items()} == {"n1": "AMD EPYC 7763 64-Core Processor", "n2": "Intel Xeon"}
    posted = [c for c in kube.calls if c[0] == "POST"]
    assert len(posted) == 2
    deleted = [p for m, p in kube.calls if m == "DELETE"]
    assert len(deleted) == 2 and all("propagationPolicy=Background" in p for p in deleted) and kube.jobs == {}  # nothing left behind


def test_the_job_is_pinned_to_its_node_runs_the_agents_own_image_with_its_pull_secrets_and_asks_for_little(monkeypatch):
    kube = Kube({"n1": X86}, pull=[{"name": "regcred"}])
    probe(kube, ["n1"], monkeypatch)
    spec = kube._gone[nodeprobe.job_name("n1")]
    pod = spec["spec"]["template"]["spec"]
    assert pod["nodeName"] == "n1" and pod["imagePullSecrets"] == [{"name": "regcred"}] and pod["restartPolicy"] == "Never"
    assert pod["containers"][0]["image"] == "ghcr.io/kaji-secure/kaji-agent:0.0.4"
    assert "/proc/cpuinfo" in pod["containers"][0]["command"][-1] and "/sys/class/dmi/id/product_name" in pod["containers"][0]["command"][-1]
    assert pod["containers"][0]["resources"]["requests"]["cpu"] == "10m" and spec["spec"]["backoffLimit"] == 0
    assert spec["spec"]["ttlSecondsAfterFinished"] and runner.LABEL not in spec["metadata"]["labels"]  # not followed as a task after a restart


def test_a_node_whose_job_cannot_be_made_stays_unknown_and_the_others_answer(monkeypatch):
    kube = Kube({"n1": X86, "n2": X86}, fail_create=["n1"])
    assert list(probe(kube, ["n1", "n2"], monkeypatch)) == ["n2"]


def test_a_node_that_does_not_answer_in_time_is_left_out_and_its_job_is_removed(monkeypatch):
    kube = Kube({"n1": X86, "n2": X86}, never=["n2"])
    clock = iter(range(0, 1000, 40))
    result = probe(kube, ["n1", "n2"], monkeypatch, deadline=90, now=lambda: next(clock))
    assert list(result) == ["n1"] and kube.jobs == {}


def test_without_the_agents_own_pod_nothing_is_probed(monkeypatch):
    monkeypatch.setattr(nodeprobe.platform, "node", lambda: "somewhere-else")

    class NoPod(Kube):
        def request(self, method, path, body=None, raw=False):
            self.calls.append((method, path))
            return None

    kube = NoPod({})
    assert nodeprobe.cpu_models(kube, ["n1"]) == {} and not [c for c in kube.calls if c[0] == "POST"]


def test_the_models_are_put_on_the_nodes_in_the_report(monkeypatch):
    class Nodes:
        namespace = NS

        def request(self, method, path, body=None, raw=False):
            return {"items": [{"metadata": {"name": "n1"}, "status": {"nodeInfo": {"architecture": "amd64"}, "capacity": {"cpu": "4"}, "allocatable": {"cpu": "4"}}}]}

    out = sysinfo.collect(Nodes(), cpu_models={"n1": {"model": "AMD EPYC", "codename": "Milan (Zen 3)", "mhz": 2445.4}})
    assert out["nodes"][0]["name"] == "n1" and out["nodes"][0]["cpu_model"] == "AMD EPYC"
    assert out["nodes"][0]["cpu_info"] == {"model": "AMD EPYC", "codename": "Milan (Zen 3)", "mhz": 2445.4}
    assert "cpu_model" not in sysinfo.collect(Nodes())["nodes"][0]


async def test_when_asked_the_agent_answers_at_once_and_again_with_the_cpus_when_the_probes_are_done(monkeypatch):
    monkeypatch.setattr(sysinfo, "collect", lambda kube, cpu_models=None, **kw: {"nodes": [{"name": "n1", **({"cpu_model": cpu_models["n1"]["model"]} if cpu_models else {})}]})
    monkeypatch.setattr(nodeprobe, "cpu_models", lambda kube, names: {"n1": {"model": "AMD EPYC"}})
    got = []

    async def gateway(ws):
        await ws.recv()  # hello
        await ws.send(json.dumps({"type": "inspect"}))
        for _ in range(2):
            got.append(json.loads(await ws.recv()))
        await ws.close()

    class K:
        namespace = NS

        def request(self, *a, **k):
            return {"items": []}

    server = await websockets.serve(gateway, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    async with websockets.connect(url) as ws:
        await asyncio.wait_for(Agent(url, "kb_x", kube=K()).session(ws), 5)
    server.close()
    assert [m["type"] for m in got] == ["hardware", "hardware"]
    assert "cpu_model" not in got[0]["hardware"]["nodes"][0] and got[1]["hardware"]["nodes"][0]["cpu_model"] == "AMD EPYC"
