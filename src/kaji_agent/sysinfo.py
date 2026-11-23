"""What the machine is: the CPU, the memory and the disk, read when kaji asks (the console's *Refresh status*), never on a timer.

The agent runs in a pod, so ``/proc`` and the filesystem it sees are those of the node it runs on: the numbers are that node's, not the pod's limits. For a
cluster of several nodes, the nodes themselves can be listed too, if the agent was given the right to read them (``nodes: true`` in the chart); without it that part
is left out, and nothing else changes.
"""

import os
import shutil

from .kube import KubeError

MAX_NODES = 50
MODEL_KEYS = (
    "model name",
    "Model name",
    "Hardware",
    "Processor",
    "cpu model",
    "machine",
)  # x86; arm, power and s390 name it differently


def cpu(proc="/proc"):
    """``(model, cores)``: the model of the first CPU as the kernel names it (empty if it does not), and the cores this process may run on."""
    model = ""
    try:
        with open(f"{proc}/cpuinfo") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key.strip() in MODEL_KEYS and value.strip():
                    model = " ".join(value.split())
                    break
    except OSError:
        pass
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cores = os.cpu_count() or 0
    return model, cores


def memory(proc="/proc"):
    """``(total, available)`` in bytes, from ``/proc/meminfo``; ``None`` for what is not there."""
    found = {}
    try:
        with open(f"{proc}/meminfo") as f:
            for line in f:
                name, _, rest = line.partition(":")
                parts = rest.split()
                if name in ("MemTotal", "MemAvailable") and parts and parts[0].isdigit():
                    found[name] = int(parts[0]) * 1024  # the file says kB, and means KiB
    except OSError:
        pass
    return found.get("MemTotal"), found.get("MemAvailable")


def disk(path="/"):
    """``(total, free)`` in bytes of the filesystem under ``path`` (what a task has to write to), or ``(None, None)``."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None, None
    return usage.total, usage.free


UNITS = {
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "k": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "m": 1e-3,
}


def quantity(text):
    """A Kubernetes quantity (``4``, ``3920m``, ``16Gi``, ``16384Ki``) as a number; ``None`` if it is not one."""
    text = str(text or "").strip()
    for suffix in sorted(UNITS, key=len, reverse=True):
        if text.endswith(suffix) and text[: -len(suffix)].replace(".", "", 1).isdigit():
            return float(text[: -len(suffix)]) * UNITS[suffix]
    try:
        return float(text)
    except ValueError:
        return None


def nodes(kube):
    """The nodes of the cluster the agent runs in, or ``None`` if it may not list them. Only what a node says of itself: nothing about what runs on it."""
    try:
        listed = (kube.request("GET", "/api/v1/nodes") or {}).get("items", [])
    except KubeError as err:
        if err.status in (401, 403, 404):
            return None
        raise
    out = []
    for node in listed[:MAX_NODES]:
        status, info = node.get("status") or {}, (node.get("status") or {}).get("nodeInfo") or {}
        cap, alloc = status.get("capacity") or {}, status.get("allocatable") or {}
        out.append(
            {
                "name": (node.get("metadata") or {}).get("name", ""),
                "arch": info.get("architecture", ""),
                "kubelet": info.get("kubeletVersion", ""),
                "cpu": quantity(cap.get("cpu")),
                "memory": int(quantity(cap.get("memory")) or 0) or None,
                "storage": int(quantity(cap.get("ephemeral-storage")) or 0) or None,
                "cpu_allocatable": quantity(alloc.get("cpu")),
                "memory_allocatable": int(quantity(alloc.get("memory")) or 0) or None,
            }
        )
    return out


def collect(kube=None, proc="/proc", path="/"):
    """Everything, as the message to kaji: the machine the agent is on, and, if it may, the nodes of its cluster."""
    model, cores = cpu(proc)
    total, available = memory(proc)
    disk_total, disk_free = disk(path)
    out = {
        "cpu_model": model,
        "cpu_cores": cores,
        "memory_total": total,
        "memory_available": available,
        "disk_total": disk_total,
        "disk_free": disk_free,
    }
    try:
        out["kernel"] = os.uname().release
    except AttributeError:
        pass
    if kube is not None and (listed := nodes(kube)) is not None:
        out["nodes"] = listed
    return {k: v for k, v in out.items() if v not in (None, "")}
