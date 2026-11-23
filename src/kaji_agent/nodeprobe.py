"""What CPU each node of the cluster has.

A Kubernetes Node does not say: its object holds the architecture and the capacity, no CPU model, and the agent's own pod sees only the one node it
runs on. So when the console asks what the cluster is (never on a timer), the agent runs a short Job on each node that reads ``/proc/cpuinfo`` and
reads the answer from its log. The Job runs the agent's own image (found from the agent's own pod, with its pull secrets), is pinned to its node
(``nodeName``), asks for next to nothing, and is removed as soon as its answer is read (``ttlSecondsAfterFinished`` as a safety net). It needs the
Role's ``create`` and ``delete`` on jobs and ``list`` on pods and their logs, which the agent has. Whatever goes wrong leaves that node's CPU unknown."""

import hashlib
import logging
import platform
import time

log = logging.getLogger("kaji_agent")

PROBE_LABEL = "kaji.dev/probe"  # not the task label: a restarted agent follows the Jobs that carry that one (runner.LABEL)
KEYS = ("model name", "Model name", "Hardware", "cpu model")
COMMAND = ["sh", "-c", "grep -m1 -E '^(" + "|".join(KEYS) + ")' /proc/cpuinfo || true"]
DEADLINE = 90  # seconds, for all the nodes together: a node that has to pull the image first may need most of it
POLL = 2


def job_name(node):
    return "kaji-cpu-" + hashlib.sha1(node.encode()).hexdigest()[:10]


def own_pod(kube):
    """``(image, imagePullSecrets)`` of the pod this agent runs in, or ``None`` outside a cluster or without the right to read it."""
    try:
        pod = kube.request("GET", f"/api/v1/namespaces/{kube.namespace}/pods/{platform.node()}")
    except OSError:
        return None
    if not pod:
        return None
    spec = pod.get("spec") or {}
    containers = spec.get("containers") or []
    return (containers[0].get("image"), spec.get("imagePullSecrets") or []) if containers and containers[0].get("image") else None


def probe_job(kube, node, image, pull_secrets):
    labels = {PROBE_LABEL: "cpu"}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name(node), "namespace": kube.namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": DEADLINE,
            "ttlSecondsAfterFinished": 120,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeName": node,
                    "imagePullSecrets": pull_secrets,
                    "containers": [
                        {
                            "name": "cpu",
                            "image": image,
                            "command": COMMAND,
                            "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}, "limits": {"memory": "32Mi"}},
                            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                        }
                    ],
                },
            },
        },
    }


def parse(log_text):
    """The CPU model from the line ``model name<TAB>: AMD EPYC ...`` that was read, tidied (spaces), or ``""``."""
    for line in (log_text or "").splitlines():
        key, _, value = line.partition(":")
        if key.strip() in KEYS and value.strip():
            return " ".join(value.split())
    return ""


def cpu_models(kube, nodes, deadline=DEADLINE, poll=POLL, now=time.monotonic, sleep=time.sleep):
    """``{node name: CPU model}`` for the nodes that answered in time. Blocking: the agent calls it in a thread."""
    found = own_pod(kube) if nodes else None
    if found is None:
        return {}
    image, pull_secrets = found
    base = f"/apis/batch/v1/namespaces/{kube.namespace}/jobs"
    pending = {}
    for node in nodes:
        try:
            kube.request("POST", base, probe_job(kube, node, image, pull_secrets))
            pending[node] = job_name(node)
        except OSError as err:  # not allowed, or the Job is there from a probe that was cut short: that node stays unknown
            log.warning("could not probe the CPU of %s: %s", node, err)
    models, end = {}, now() + deadline
    try:
        while pending and now() < end:
            for node, name in list(pending.items()):
                job = kube.request("GET", f"{base}/{name}") or {}
                status = job.get("status") or {}
                if not (status.get("succeeded") or status.get("failed")):
                    continue
                del pending[node]
                if status.get("succeeded"):
                    pods = (kube.request("GET", f"/api/v1/namespaces/{kube.namespace}/pods?labelSelector=job-name%3D{name}") or {}).get("items", [])
                    if pods:
                        text = kube.request("GET", f"/api/v1/namespaces/{kube.namespace}/pods/{pods[0]['metadata']['name']}/log", raw=True)
                        if model := parse(text):
                            models[node] = model
            if pending:
                sleep(poll)
    finally:
        for name in [job_name(n) for n in nodes]:  # the answers are read: the Jobs are not needed (or are too slow: stop waiting for them)
            try:
                kube.request("DELETE", f"{base}/{name}?propagationPolicy=Background")
            except OSError:
                pass
    return models
