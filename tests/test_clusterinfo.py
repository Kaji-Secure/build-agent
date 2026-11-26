import pytest
from kaji_agent import clusterinfo
from kaji_agent.kube import KubeError


def node(name, os_image="Ubuntu 24.04.1 LTS", **labels):
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {
            "nodeInfo": {
                "osImage": os_image,
                "operatingSystem": "linux",
                "architecture": "amd64",
                "kernelVersion": "6.8.0-45-generic",
                "kubeletVersion": "v1.31.1",
                "containerRuntimeVersion": "containerd://1.7.22",
            }
        },
    }


def pod(name, owner, images, namespace="default", phase="Running", init=()):
    def status(image, digest):
        return {"name": "c", "image": image, "imageID": f"docker-pullable://{digest}" if digest else ""}

    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            **({"ownerReferences": [{"kind": owner[0], "name": owner[1], "controller": True}]} if owner else {}),
        },
        "status": {
            "phase": phase,
            "containerStatuses": [status(*i) for i in images],
            "initContainerStatuses": [status(*i) for i in init],
        },
    }


class FakeKube:
    namespace = "kaji"

    def __init__(self, nodes=(), pods=(), version="v1.31.1", fail=None):
        self.nodes, self.pods, self.version, self.fail = list(nodes), list(pods), version, fail
        self.calls = []

    def request(self, method, path, body=None, raw=False):
        self.calls.append(path)
        if "/jobs" in path:  # the runner looks for the Jobs of tasks it was running before
            return {"items": []}
        if self.fail:
            raise self.fail
        if path == "/version":
            return {"gitVersion": self.version}
        if path.startswith("/api/v1/nodes"):
            return {"items": self.nodes}
        if path.startswith("/api/v1/pods"):
            # two pages, to show the agent follows `continue`
            if "continue=more" in path:
                return {"items": self.pods[1:], "metadata": {}}
            return {"items": self.pods[:1], "metadata": {"continue": "more"}}
        raise AssertionError(path)


NGINX = "docker.io/library/nginx@sha256:" + "a" * 64


def test_a_node_is_its_os_kernel_kubelet_and_runtime_and_nothing_else():
    got = clusterinfo.nodes(FakeKube(nodes=[node("w1")]))
    assert got == [
        {
            "name": "w1",
            "os_image": "Ubuntu 24.04.1 LTS",
            "os": "linux",
            "arch": "amd64",
            "kernel": "6.8.0-45-generic",
            "kubelet": "v1.31.1",
            "runtime": "containerd://1.7.22",
            "control_plane": False,
        }
    ]


def test_every_node_counts_for_the_inventory_a_control_plane_too_and_is_marked():
    got = clusterinfo.nodes(FakeKube(nodes=[node("cp", **{"node-role.kubernetes.io/control-plane": ""})]))
    assert got[0]["control_plane"] is True


def test_an_image_is_listed_once_with_its_digest_the_workloads_using_it_and_the_pods():
    kube = FakeKube(
        pods=[
            pod("web-6d4cf56db6-abcde", ("ReplicaSet", "web-6d4cf56db6"), [("nginx:1.27", NGINX)]),
            pod("web-6d4cf56db6-fghij", ("ReplicaSet", "web-6d4cf56db6"), [("nginx:1.27", NGINX)]),
            pod("batch-29412345-xyz12", ("Job", "batch-29412345"), [("nginx:1.27", NGINX)], namespace="jobs"),
        ]
    )
    (image,) = clusterinfo.images(kube)
    assert image["image"] == "nginx:1.27" and image["digest"] == "sha256:" + "a" * 64
    assert image["pods"] == 3
    assert image["workloads"] == [
        {"namespace": "default", "kind": "Deployment", "name": "web"},
        {"namespace": "jobs", "kind": "CronJob", "name": "batch"},
    ]


def test_pages_of_pods_are_followed():
    kube = FakeKube(pods=[pod("a", None, [("a:1", "")]), pod("b", None, [("b:1", "")])])
    assert [i["image"] for i in clusterinfo.images(kube)] == ["a:1", "b:1"]
    assert any("continue=more" in c for c in kube.calls)


def test_init_containers_count_and_pods_that_are_not_running_do_not():
    kube = FakeKube(
        pods=[
            pod("a", ("StatefulSet", "db"), [("db:1", "")], init=[("busybox:1", "")]),
            pod("b", ("StatefulSet", "db"), [("old:1", "")], phase="Succeeded"),
        ]
    )
    assert {i["image"] for i in clusterinfo.images(kube)} == {"db:1", "busybox:1"}


def test_an_image_without_a_digest_has_an_empty_one_and_a_bare_pod_names_itself():
    (image,) = clusterinfo.images(FakeKube(pods=[pod("solo", None, [("x:1", "")])]))
    assert image["digest"] == "" and image["workloads"] == [{"namespace": "default", "kind": "Pod", "name": "solo"}]


def test_a_cluster_that_runs_too_many_images_is_cut_not_sent_whole(monkeypatch):
    monkeypatch.setattr(clusterinfo, "MAX_IMAGES", 2)
    kube = FakeKube(pods=[pod(f"p{i}", None, [(f"i{i}:1", "")]) for i in range(5)])
    assert len(clusterinfo.images(kube)) == 2


def test_the_report_has_the_version_the_nodes_and_the_images():
    kube = FakeKube(nodes=[node("w1")], pods=[pod("a", None, [("a:1", "")])])
    report = clusterinfo.report(kube)
    assert report["version"] == "v1.31.1" and len(report["nodes"]) == 1 and len(report["images"]) == 1


def test_what_the_agent_may_not_read_is_left_out_not_fatal():
    forbidden = KubeError(403, "forbidden")
    assert clusterinfo.report(FakeKube(fail=forbidden)) == {}


def test_other_errors_are_not_hidden():
    with pytest.raises(KubeError):
        clusterinfo.report(FakeKube(fail=KubeError(500, "boom")))


# ---------------------------------------------------------------- what the builder agent does with it
import asyncio
import json

import websockets
from kaji_agent.client import Agent


async def run_session(messages_from_gateway, kube, role="builder", env=None, monkeypatch=None, wanted=1):
    got = []

    async def gateway(ws):
        await ws.recv()  # hello
        for m in messages_from_gateway:
            await ws.send(json.dumps(m))
        while len([g for g in got if g["type"] == "report"]) < wanted:
            try:
                got.append(json.loads(await asyncio.wait_for(ws.recv(), 0.5)))
            except asyncio.TimeoutError:
                break
        await ws.close()

    server = await websockets.serve(gateway, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    async with websockets.connect(url) as ws:
        await asyncio.wait_for(Agent(url, "kb_x", kube=kube, role=role).session(ws), 5)
    server.close()
    return [g for g in got if g["type"] == "report"]


async def test_a_builder_reports_its_cluster_when_it_is_welcomed():
    kube = FakeKube(nodes=[node("w1")], pods=[pod("a", None, [("a:1", "")])])
    (message,) = await run_session([{"type": "welcome"}], kube)
    assert message["kind"] == "cluster" and message["report"]["nodes"][0]["name"] == "w1"
    assert message["report"]["images"][0]["image"] == "a:1"


async def test_a_builder_reports_its_cluster_again_when_the_console_asks():
    kube = FakeKube(nodes=[node("w1")], pods=[pod("a", None, [("a:1", "")])])
    got = await run_session([{"type": "welcome"}, {"type": "inspect"}], kube, wanted=2)
    assert [m["kind"] for m in got] == ["cluster", "cluster"]


async def test_a_builder_that_may_not_list_pods_says_nothing_and_goes_on():
    assert await run_session([{"type": "welcome"}], FakeKube(fail=KubeError(403, "forbidden"))) == []


async def test_the_cluster_report_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("KAJI_CLUSTER_REPORT", "0")
    assert await run_session([{"type": "welcome"}], FakeKube(nodes=[node("w1")])) == []


async def test_a_builder_without_a_cluster_has_nothing_to_report():
    assert await run_session([{"type": "welcome"}], None) == []
