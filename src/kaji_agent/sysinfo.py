"""What the machine is: the CPU, the memory and the disk, read when kaji asks (the console's *Refresh status*), never on a timer.

The agent runs in a pod, so ``/proc`` and the filesystem it sees are those of the node it runs on: the numbers are that node's, not the pod's limits. For a
cluster of several nodes, the nodes themselves can be listed too, if the agent was given the right to read them (``nodes: true`` in the chart); without it that part
is left out, and nothing else changes.
"""

import glob
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


def topology(sys_cpu="/sys/devices/system/cpu"):
    """``(cores, threads)`` of the machine: its physical cores and its logical CPUs (hardware threads), from the kernel's topology, or ``None`` when
    it does not say. A CPU that is off has no topology and is not counted. kaji's quota is in cores: with two threads to a core, 16 cores are 32 CPUs."""
    cores, threads = set(), 0
    for cpu_dir in glob.glob(f"{sys_cpu}/cpu[0-9]*"):
        try:
            with open(f"{cpu_dir}/topology/physical_package_id") as f:
                package = f.read().strip()
            with open(f"{cpu_dir}/topology/core_id") as f:
                core = f.read().strip()
        except OSError:
            continue
        cores.add((package, core))
        threads += 1
    return (len(cores), threads) if cores else None


def threads_per_core(sys_cpu="/sys/devices/system/cpu"):
    """How many logical CPUs a core has here (1 without hyper-threading, usually 2 with); 1 when the kernel does not say."""
    found = topology(sys_cpu)
    return found[1] / found[0] if found else 1.0


def cgroup_cpu_limit(root="/sys/fs/cgroup"):
    """The CPU limit this container runs under, in logical CPUs (``limits.cpu`` of its pod), or ``None`` when it has none."""
    try:
        with open(f"{root}/cpu.max") as f:  # cgroup v2: "<quota> <period>", or "max <period>"
            quota, _, period = f.read().strip().partition(" ")
        return int(quota) / int(period) if quota != "max" else None
    except (OSError, ValueError):
        pass
    try:
        with open(f"{root}/cpu/cpu.cfs_quota_us") as f:  # cgroup v1
            quota = int(f.read())
        with open(f"{root}/cpu/cpu.cfs_period_us") as f:
            period = int(f.read())
        return quota / period if quota > 0 and period > 0 else None
    except (OSError, ValueError):
        return None


def cpu_budget(environ=None, cgroup_root="/sys/fs/cgroup", sys_cpu="/sys/devices/system/cpu"):
    """``(cores, where from)``: how many CPU **cores** the user gave kaji to build with, or ``(None, "")`` if they did not say. ``KAJI_CPUS`` is in
    cores; the CPU limit of the agent's container (``resources.limits.cpu``, in logical CPUs) is turned into cores with the threads per core. The
    gateway holds the builds to it and counts it against the plan (docs/builder-agent.rst)."""
    environ = os.environ if environ is None else environ
    try:
        declared = float(environ.get("KAJI_CPUS") or 0)
    except ValueError:
        declared = 0
    if declared > 0:
        return declared, "KAJI_CPUS"
    limit = cgroup_cpu_limit(cgroup_root)
    if limit:
        return round(limit / threads_per_core(sys_cpu), 2), "container limit"
    return None, ""


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


def schedulable(node):
    """Can a task's pod run on this node? Its Jobs tolerate no taint, so a node that is cordoned or has a NoSchedule or NoExecute taint (the control
    plane of most clusters) never gets one, however big it is; nor does one that is not Ready (a node that does not say is taken as ready), nor one the user labelled ``kaji=NoBuilder``."""
    spec = node.get("spec") or {}
    ready = [c.get("status") for c in (node.get("status") or {}).get("conditions") or [] if c.get("type") == "Ready"]
    labels = (node.get("metadata") or {}).get("labels") or {}
    return (
        not spec.get("unschedulable")
        and labels.get("kaji") != "NoBuilder"  # the user's own switch: a node labelled kaji=NoBuilder takes no kaji work
        and ready in ([], ["True"])
        and not any(t.get("effect") in ("NoSchedule", "NoExecute") for t in spec.get("taints") or [])
    )


def nodes(kube):
    """The nodes of the cluster the agent runs in that can take a build (a control plane or a cordoned node is left out), or ``None`` if it may
    not list them. Only what a node says of itself: nothing about what runs on it."""
    try:
        listed = (kube.request("GET", "/api/v1/nodes") or {}).get("items", [])
    except KubeError as err:
        if err.status in (401, 403, 404):
            return None
        raise
    out = []
    for node in [n for n in listed if schedulable(n)][:MAX_NODES]:
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


def collect(kube=None, proc="/proc", path="/", cpu_models=None):
    """Everything, as the message to kaji: the machine the agent is on, and, if it may, the nodes of its cluster. ``cpu_models`` is what the probes
    of the nodes found (``nodeprobe.cpu_models``): each node that answered has its CPU model."""
    model, cores = cpu(proc)
    total, available = memory(proc)
    disk_total, disk_free = disk(path)
    found = topology()
    out = {
        "cpu_model": model,
        "cpu_cores": cores,  # the logical CPUs this process may run on
        "cpu_physical_cores": found[0] if found else None,
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
        for node in listed:
            if model := (cpu_models or {}).get(node.get("name")):
                node["cpu_model"] = model
        out["nodes"] = listed
    return {k: v for k, v in out.items() if v not in (None, "")}
