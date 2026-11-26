"""What the cluster the agent runs in is: its Kubernetes version, the operating system of each node and the images its pods run.

Read-only and bounded: only what nodes and pods say of themselves (names, the OS and versions of a node, the images of the containers and their digests).
Environment, arguments, volumes and secrets are never read into the report. The agent needs ``list`` on ``nodes`` and ``pods`` for all namespaces
(the chart's ``clusterReport``); without it ``report`` is empty and nothing else changes.
"""

import re
from urllib.parse import quote

from .kube import KubeError

MAX_IMAGES = 2000
MAX_WORKLOADS = 50  # of one image
PAGE = 500  # pods asked for at a time: a big cluster is not one huge answer
CONTROL_PLANE = "node-role.kubernetes.io/control-plane"
REPLICASET = re.compile(r"^(.+)-[a-z0-9]{5,10}$")  # a Deployment's ReplicaSet: <deployment>-<pod-template-hash>
JOB = re.compile(r"^(.+)-\d{6,}$")  # a CronJob's Job: <cronjob>-<scheduled time in minutes>
DIGEST = re.compile(r"@(sha256:[0-9a-f]{64})$")


def _get(kube, path):
    """The answer as JSON, or ``None`` when the agent may not read it (401, 403, 404)."""
    try:
        return kube.request("GET", path)
    except KubeError as err:
        if err.status in (401, 403, 404):
            return None
        raise


def nodes(kube):
    """Every node, control plane included (this is an inventory, not what can take a build): the OS image, kernel, kubelet and container runtime it says of itself.
    ``None`` if the agent may not list them."""
    listed = _get(kube, "/api/v1/nodes")
    if listed is None:
        return None
    out = []
    for n in listed.get("items", []):
        info = (n.get("status") or {}).get("nodeInfo") or {}
        out.append(
            {
                "name": (n.get("metadata") or {}).get("name", ""),
                "os_image": info.get("osImage", ""),
                "os": info.get("operatingSystem", ""),
                "arch": info.get("architecture", ""),
                "kernel": info.get("kernelVersion", ""),
                "kubelet": info.get("kubeletVersion", ""),
                "runtime": info.get("containerRuntimeVersion", ""),
                "control_plane": CONTROL_PLANE in ((n.get("metadata") or {}).get("labels") or {}),
            }
        )
    return out


def _workload(pod):
    """``(kind, name)`` of what runs the pod. A Deployment is two controllers away (pod, ReplicaSet, Deployment) and a CronJob likewise; the agent has no
    right to read ReplicaSets and Jobs, so it takes them from the names Kubernetes gives them. A pod with no controller is its own workload."""
    meta = pod.get("metadata") or {}
    owners = [o for o in meta.get("ownerReferences") or [] if o.get("controller")] or meta.get("ownerReferences") or []
    if not owners:
        return "Pod", meta.get("name", "")
    kind, name = owners[0].get("kind", ""), owners[0].get("name", "")
    if kind == "ReplicaSet" and (m := REPLICASET.match(name)):
        return "Deployment", m.group(1)
    if kind == "Job" and (m := JOB.match(name)):
        return "CronJob", m.group(1)
    return kind, name


def _pods(kube):
    """The pods of all namespaces, a page at a time; ``None`` if the agent may not list them."""
    token, found = "", []
    while True:
        page = _get(kube, f"/api/v1/pods?limit={PAGE}" + (f"&continue={quote(token)}" if token else ""))
        if page is None:
            return None
        found += page.get("items", [])
        token = (page.get("metadata") or {}).get("continue") or ""
        if not token:
            return found


def images(kube):
    """The images running in the cluster, each once: ``{image, digest, pods, workloads: [{namespace, kind, name}]}``. The digest is what the container
    runtime resolved the image to (``imageID``); empty when it does not say. Only running pods count. ``None`` if the agent may not list pods."""
    pods = _pods(kube)
    if pods is None:
        return None
    found = {}
    for pod in pods:
        status = pod.get("status") or {}
        if status.get("phase") != "Running":
            continue
        kind, name = _workload(pod)
        workload = {"namespace": (pod.get("metadata") or {}).get("namespace", ""), "kind": kind, "name": name}
        for c in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
            if not c.get("image"):
                continue
            digest = m.group(1) if (m := DIGEST.search(c.get("imageID") or "")) else ""
            key = (c["image"], digest)
            if key not in found and len(found) >= MAX_IMAGES:
                continue
            entry = found.setdefault(key, {"image": c["image"], "digest": digest, "pods": 0, "workloads": []})
            entry["pods"] += 1
            if workload not in entry["workloads"] and len(entry["workloads"]) < MAX_WORKLOADS:
                entry["workloads"].append(workload)
    return [found[k] for k in sorted(found)]


def report(kube):
    """The cluster as the message to kaji: ``{version, nodes, images}``; ``{}`` if the agent may not read what it needs."""
    listed, running = nodes(kube), images(kube)
    if listed is None or running is None:
        return {}
    version = (_get(kube, "/version") or {}).get("gitVersion", "")
    return {"version": version, "nodes": listed, "images": running}
