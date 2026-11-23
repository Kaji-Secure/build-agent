import json

import websockets
from kaji_agent import sysinfo
from kaji_agent.client import Agent
from kaji_agent.kube import KubeError

X86 = """processor\t: 0
vendor_id\t: GenuineIntel
model name\t: AMD EPYC 7763 64-Core   Processor
cpu MHz\t\t: 2450.000

processor\t: 1
model name\t: AMD EPYC 7763 64-Core   Processor
"""
ARM = "processor\t: 0\nBogoMIPS\t: 50.00\nFeatures\t: fp asimd\nCPU implementer\t: 0x41\nHardware\t: Rockchip RK3588\n"
MEMINFO = (
    "MemTotal:       16384000 kB\nMemFree:         1000000 kB\nMemAvailable:    8192000 kB\nBuffers:           1 kB\n"
)


def proc(tmp_path, cpuinfo=X86, meminfo=MEMINFO):
    (tmp_path / "cpuinfo").write_text(cpuinfo)
    (tmp_path / "meminfo").write_text(meminfo)
    return str(tmp_path)


def test_the_cpu_model_is_the_kernels_name_for_the_first_cpu_with_its_spaces_tidied(tmp_path):
    model, cores = sysinfo.cpu(proc(tmp_path))
    assert model == "AMD EPYC 7763 64-Core Processor" and cores >= 1


def test_an_arm_machine_names_its_cpu_differently(tmp_path):
    assert sysinfo.cpu(proc(tmp_path, ARM))[0] == "Rockchip RK3588"


def test_a_kernel_that_does_not_say_leaves_the_model_empty(tmp_path):
    assert sysinfo.cpu(proc(tmp_path, "processor\t: 0\n"))[0] == "" and sysinfo.cpu(str(tmp_path / "nowhere"))[0] == ""


def test_memory_is_in_bytes_although_the_file_says_kb(tmp_path):
    assert sysinfo.memory(proc(tmp_path)) == (16384000 * 1024, 8192000 * 1024)
    assert sysinfo.memory(str(tmp_path / "nowhere")) == (None, None)
    assert sysinfo.memory(proc(tmp_path, meminfo="MemTotal: 5 kB\n")) == (
        5 * 1024,
        None,
    )  # an old kernel has no MemAvailable


def test_the_disk_is_the_filesystem_under_the_path(tmp_path):
    total, free = sysinfo.disk(str(tmp_path))
    assert total > 0 and 0 <= free <= total and sysinfo.disk(str(tmp_path / "nowhere")) == (None, None)


def test_kubernetes_quantities():
    assert sysinfo.quantity("4") == 4 and abs(sysinfo.quantity("3920m") - 3.92) < 1e-9
    assert (
        sysinfo.quantity("16Gi") == 16 * 2**30
        and sysinfo.quantity("16384Ki") == 16384 * 1024
        and sysinfo.quantity("2G") == 2 * 10**9
    )
    assert sysinfo.quantity("") is None and sysinfo.quantity("lots") is None and sysinfo.quantity(None) is None


NODE = {
    "metadata": {"name": "n1"},
    "status": {
        "nodeInfo": {"architecture": "arm64", "kubeletVersion": "v1.33.1"},
        "capacity": {"cpu": "8", "memory": "32Gi", "ephemeral-storage": "200Gi"},
        "allocatable": {"cpu": "7910m", "memory": "31Gi"},
    },
}


class Kube:
    def __init__(self, answer):
        self.answer = answer

    def request(self, method, path, body=None, raw=False):
        if isinstance(self.answer, Exception):
            raise self.answer
        assert (method, path) == ("GET", "/api/v1/nodes")
        return self.answer


def test_the_nodes_of_the_cluster_say_what_they_are():
    (node,) = sysinfo.nodes(Kube({"items": [NODE]}))
    assert node == {
        "name": "n1",
        "arch": "arm64",
        "kubelet": "v1.33.1",
        "cpu": 8.0,
        "memory": 32 * 2**30,
        "storage": 200 * 2**30,
        "cpu_allocatable": 7.91,
        "memory_allocatable": 31 * 2**30,
    }


def test_without_the_right_to_list_nodes_that_part_is_left_out_and_nothing_else_fails():
    assert sysinfo.nodes(Kube(KubeError(403, "forbidden"))) is None
    assert "nodes" not in sysinfo.collect(Kube(KubeError(403, "forbidden")))
    try:
        sysinfo.nodes(Kube(KubeError(500, "boom")))
    except KubeError:
        pass
    else:
        raise AssertionError("a server error is not a missing permission")


def test_no_more_nodes_than_the_limit_are_sent():
    assert len(sysinfo.nodes(Kube({"items": [NODE] * 80}))) == sysinfo.MAX_NODES


def test_collect_is_one_flat_message_without_the_empty_parts(tmp_path):
    got = sysinfo.collect(Kube({"items": [NODE]}), proc(tmp_path), str(tmp_path))
    assert (
        got["cpu_model"] == "AMD EPYC 7763 64-Core Processor"
        and got["memory_total"] == 16384000 * 1024
        and got["disk_free"] > 0
        and got["nodes"][0]["name"] == "n1"
    )
    assert "cpu_model" not in sysinfo.collect(None, str(tmp_path / "nowhere"), str(tmp_path / "nowhere"))


async def test_the_agent_answers_inspect_with_its_hardware_and_does_not_volunteer_it():
    seen = []

    async def gateway(ws):
        seen.append(json.loads(await ws.recv())["type"])  # hello: no hardware in it
        await ws.send(json.dumps({"type": "welcome"}))
        await ws.send(json.dumps({"type": "inspect"}))
        reply = json.loads(await ws.recv())
        seen.append(reply)
        await ws.close()

    server = await websockets.serve(gateway, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    async with websockets.connect(url) as ws:
        await Agent(url, "kb_x").session(ws)
    server.close()
    assert (
        seen[0] == "hello"
        and seen[1]["type"] == "hardware"
        and seen[1]["hardware"]["cpu_cores"] >= 1
        and "disk_total" in seen[1]["hardware"]
    )
