"""Run a task as a Kubernetes Job in the agent's own cluster, and report how it went.

The task's payload is what the operator would have made a Job of: the image, the command, the worker's environment and its resources.
The worker ends with a JSON termination message (what it built, reused, copied); that and the tail of its log are the task's result.
"""

import asyncio
import json
import logging
import re

# A node the user labelled ``kaji: NoBuilder`` gets none of this agent's pods (nodes without the label are fine); the operator does the same.
NO_BUILDER_NODES = {
    "affinity": {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [{"matchExpressions": [{"key": "kaji", "operator": "NotIn", "values": ["NoBuilder"]}]}]
            }
        }
    }
}


LABEL = "kaji.systems/task"
LOG_TAIL = 200  # lines of the worker's log sent with the result
LOG_EVERY = 10  # seconds between the log tails sent while a task runs
LIVE_LOG_MAX = 32000  # characters of such a tail: the end of it
POLL_SECONDS = 5
DRY_RUN_CPU = "100m"
BINFMT_NAMES = {
    "arm64": "arm64",
    "arm": "arm",
    "riscv": "riscv64",
    "amd64": "amd64",
}  # the emulator's name for an architecture
# rootless podman makes its own user namespace: it runs as a plain user, but newuidmap must be able to gain the capability to write the id maps, and
# the seccomp and AppArmor profiles must allow unshare. The one security context a task may ask for (``rootless``), and no other.
ROOTLESS = {
    "runAsUser": 1000,
    "runAsGroup": 1000,
    "runAsNonRoot": True,
    "allowPrivilegeEscalation": True,
    "seccompProfile": {"type": "Unconfined"},
    "appArmorProfile": {"type": "Unconfined"},
}
TASK_TOKEN = re.compile(r"kt1\.\d+\.[0-9a-f]{40,}")  # the token a task reads and uploads with (proxy.mint_task_token)
log = logging.getLogger("kaji_agent")


def redact(text):
    """The log of a task, without its tokens: pap prints the address it reads from, login and all, and the log goes to the web app and the build's page."""
    return TASK_TOKEN.sub("kt1.***", text)


def job_name(uid):
    return f"kaji-{uid[:13]}"


def volume_for(name, v):
    """``(volume, mount)`` for one entry of a task's ``volumes``: ``kind`` is ``empty`` (``size``), ``image`` (``image``: the tree), ``config``
    (a ConfigMap of ``data``) or ``secret`` (a Secret of ``data``); the last two are made by ``extras_for``."""
    kind = v["kind"]
    if kind == "empty":
        volume = {"emptyDir": {"sizeLimit": v["size"]} if v.get("size") else {}}
    elif kind == "image":
        volume = {"image": {"reference": v["image"], "pullPolicy": "IfNotPresent"}}
    elif kind == "config":
        volume = {"configMap": {"name": f"{name}-{v['name']}"}}
    elif kind == "secret":
        volume = {"secret": {"secretName": f"{name}-{v['name']}"}}
    else:
        raise ValueError(f"unknown volume kind {kind}")
    mount = {"name": v["name"], "mountPath": v["path"], **({"readOnly": True} if v.get("read_only") else {})}
    return {"name": v["name"], **volume}, mount


def generic_pod(name, payload):
    """The pod of a task that describes it itself: ``volumes``, ``resources`` and ``rootless``, as the operator's own Job for it has them."""
    volumes, mounts = [], []
    for v in payload["volumes"]:
        volume, mount = volume_for(name, v)
        volumes.append(volume)
        mounts.append(mount)
    container = {
        "name": "worker",
        "image": payload["image"],
        "command": payload["command"],
        "resources": payload["resources"],
        "env": [{"name": k, "value": v} for k, v in payload.get("env", {}).items()],
        "volumeMounts": mounts,
    }
    if payload.get("rootless"):
        container["securityContext"] = ROOTLESS
    return {"restartPolicy": "Never", **NO_BUILDER_NODES, "volumes": volumes, "containers": [container]}


def job_for(uid, payload, namespace):
    name = job_name(uid)
    if "volumes" in payload:
        pod = generic_pod(name, payload)
        return _job(uid, name, namespace, _with_extras(pod, name, payload))
    resources = {
        "requests": {"cpu": DRY_RUN_CPU if payload.get("dry_run") else str(payload["cpus"])},
        "limits": {"cpu": str(payload["cpus"])},
    }
    if payload.get("memory") and not payload.get("dry_run"):
        resources["requests"]["memory"] = resources["limits"]["memory"] = payload["memory"]
    mounts, volumes = (
        [{"name": "distfiles", "mountPath": "/var/cache/distfiles"}],
        [{"name": "distfiles", "emptyDir": {}}],
    )
    if payload.get("tree_image"):  # the Portage tree, as the image it is kept in
        mounts.append({"name": "tree", "mountPath": payload["tree_mount"], "readOnly": True})
        volumes.append({"name": "tree", "image": {"reference": payload["tree_image"], "pullPolicy": "IfNotPresent"}})
    if payload.get("config"):  # the Portage configuration, made a ConfigMap here (see extras_for)
        mounts.append({"name": "config", "mountPath": "/wm/config", "readOnly": True})
        volumes.append({"name": "config", "configMap": {"name": f"{name}-config"}})
    pod = {
        "restartPolicy": "Never", **NO_BUILDER_NODES,
        "volumes": volumes,
        "containers": [
            {
                "name": "worker",
                "image": payload["image"],
                "command": payload["command"],
                "resources": resources,
                "env": [{"name": k, "value": v} for k, v in payload.get("env", {}).items()],
                "volumeMounts": mounts,
            }
        ],
    }
    return _job(uid, name, namespace, _with_extras(pod, name, payload))


def _with_extras(pod, name, payload):
    """What every task's pod may also have: the architecture it must run on, the login that pulls its image, an emulator to register."""
    if payload.get("arch"):
        pod["nodeSelector"] = {"kubernetes.io/arch": payload["arch"]}
    if payload.get("pull_secret"):
        pod["imagePullSecrets"] = [{"name": f"{name}-pull"}]
    if payload.get("register_binfmt") and payload.get(
        "arch"
    ):  # node-wide and privileged, and only because the build asked for it
        pod["initContainers"] = [
            {
                "name": "binfmt",
                "image": payload["binfmt_image"],
                "args": ["--install", BINFMT_NAMES.get(payload["arch"], payload["arch"])],
                "securityContext": {"privileged": True},
            }
        ]
    return pod


def _job(uid, name, namespace, pod):
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": {LABEL: uid}},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 3600,
            "template": {"metadata": {"labels": {LABEL: uid}}, "spec": pod},
        },
    }


def extras_for(uid, payload, namespace, job):
    """The ConfigMap and the pull Secret the Job refers to, owned by it so that they go when it does."""
    name = job["metadata"]["name"]
    meta = {
        "namespace": namespace,
        "labels": {LABEL: uid},
        "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": name, "uid": job["metadata"]["uid"]}],
    }
    out = []
    if payload.get("config") and "volumes" not in payload:
        out.append(
            (
                f"/api/v1/namespaces/{namespace}/configmaps",
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": f"{name}-config", **meta},
                    "data": payload["config"],
                },
            )
        )
    for v in payload.get("volumes", []):
        if v["kind"] == "config":
            out.append(
                (
                    f"/api/v1/namespaces/{namespace}/configmaps",
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": f"{name}-{v['name']}", **meta},
                        "data": v["data"],
                    },
                )
            )
        elif v["kind"] == "secret":
            out.append(
                (
                    f"/api/v1/namespaces/{namespace}/secrets",
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": f"{name}-{v['name']}", **meta},
                        "stringData": v["data"],
                    },
                )
            )
    if payload.get("pull_secret"):
        out.append(
            (
                f"/api/v1/namespaces/{namespace}/secrets",
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "type": "kubernetes.io/dockerconfigjson",
                    "metadata": {"name": f"{name}-pull", **meta},
                    "stringData": {".dockerconfigjson": payload["pull_secret"]},
                },
            )
        )
    return out


def finished(job):
    """``"done"``, ``"failed"`` or ``None`` (still going)."""
    st = job.get("status") or {}
    return "done" if st.get("succeeded") else "failed" if st.get("failed") else None


class Runner:
    def __init__(self, kube, send, poll=POLL_SECONDS, log_every=LOG_EVERY):
        self.kube, self.send, self.poll = kube, send, poll  # send: async callable(message dict)
        self.log_every = log_every
        self.active = set()

    def _jobs(self):
        return f"/apis/batch/v1/namespaces/{self.kube.namespace}/jobs"

    def _tail(self, uid):
        """The end of the worker's log so far, without tokens; empty while the pod has none (not scheduled, or its container not started). The console
        cannot read the pod of a task that runs here, so this is how its build page shows the log while it runs."""
        base = f"/api/v1/namespaces/{self.kube.namespace}/pods"
        try:
            pods = (self.kube.request("GET", f"{base}?labelSelector={LABEL}%3D{uid}") or {}).get("items", [])
            if not pods:
                return ""
            text = self.kube.request("GET", f"{base}/{pods[0]['metadata']['name']}/log?tailLines={LOG_TAIL}", raw=True)
        except OSError:  # a container that has not started has no log yet: that is not an error
            return ""
        return redact(text or "")[-LIVE_LOG_MAX:]

    def _result(self, uid, failed):
        """What the worker said at its end, and the tail of its log: read from the Job's pod."""
        base = f"/api/v1/namespaces/{self.kube.namespace}/pods"
        pods = (self.kube.request("GET", f"{base}?labelSelector={LABEL}%3D{uid}") or {}).get("items", [])
        if not pods:
            return {}
        pod = pods[0]
        term = ((pod.get("status") or {}).get("containerStatuses") or [{}])[0].get("state", {}).get("terminated") or {}
        result = {}
        try:
            result = json.loads(term.get("message") or "{}")
        except ValueError:
            result = {"message": term.get("message", "")}
        if not isinstance(result, dict):
            result = {}
        try:
            result["log"] = redact(
                self.kube.request("GET", f"{base}/{pod['metadata']['name']}/log?tailLines={LOG_TAIL}", raw=True) or ""
            )
        except OSError:
            pass
        if failed and term.get("reason") and "message" not in result:
            result["message"] = term["reason"]
        return result

    async def run(self, uid, payload):
        """Start the task (if it is not started) and follow it to its end. Safe to call again for a task that is already going."""
        if uid in self.active:
            return
        self.active.add(uid)
        try:
            if await asyncio.to_thread(self.kube.request, "GET", f"{self._jobs()}/{job_name(uid)}") is None:
                job = await asyncio.to_thread(
                    self.kube.request, "POST", self._jobs(), job_for(uid, payload, self.kube.namespace)
                )
                for path, obj in extras_for(
                    uid, payload, self.kube.namespace, job
                ):  # the Job first: they are owned by it
                    await asyncio.to_thread(self.kube.request, "POST", path, obj)
            await self.send({"type": "status", "task": uid, "state": "running"})
            told, next_log = "", 0.0
            while True:
                job = await asyncio.to_thread(self.kube.request, "GET", f"{self._jobs()}/{job_name(uid)}")
                if job is None:  # taken away (cancelled): there is nothing to follow and nothing to report
                    return
                if state := finished(job):
                    break
                if (now := asyncio.get_running_loop().time()) >= next_log:
                    next_log = now + self.log_every
                    text = await asyncio.to_thread(self._tail, uid)
                    if text and text != told:  # only what changed: a build that is quiet for minutes costs no traffic
                        told = text
                        try:
                            await self.send({"type": "log", "task": uid, "log": text})
                        except Exception:  # the line is down: the next status says so, as it always did; a log is not worth failing the task for
                            log.warning("task %s: the log could not be sent", uid)
                await asyncio.sleep(self.poll)
            await self.send(
                {
                    "type": "status",
                    "task": uid,
                    "state": state,
                    "result": await asyncio.to_thread(self._result, uid, state == "failed"),
                }
            )
        except Exception as err:  # the task cannot go on: say so, instead of leaving it running for ever
            log.exception("task %s failed", uid)
            await self.send(
                {
                    "type": "status",
                    "task": uid,
                    "state": "failed",
                    "result": {"message": f"the agent could not run it: {err}"},
                }
            )
        finally:
            self.active.discard(uid)

    async def cancel(self, uid):
        """kaji dropped the task (its build was deleted, retried or replanned, or its result was taken): remove its Job. The pods, the ConfigMap and the
        Secrets it made are owned by the Job and go with it, in the background. A Job that is not there is not an error."""
        await asyncio.to_thread(
            self.kube.request, "DELETE", f"{self._jobs()}/{job_name(uid)}?propagationPolicy=Background"
        )

    async def resume(self):
        """After a restart: follow again the tasks whose Jobs are still there. Reporting an end twice is harmless."""
        jobs = (await asyncio.to_thread(self.kube.request, "GET", f"{self._jobs()}?labelSelector={LABEL}") or {}).get(
            "items", []
        )
        return [
            asyncio.create_task(self.run(j["metadata"]["labels"][LABEL], {}))
            for j in jobs
            if j["metadata"].get("labels", {}).get(LABEL)
        ]
