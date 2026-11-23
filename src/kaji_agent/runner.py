"""Run a task as a Kubernetes Job in the agent's own cluster, and report how it went.

The task's payload is what the operator would have made a Job of: the image, the command, the worker's environment and its resources.
The worker ends with a JSON termination message (what it built, reused, copied); that and the tail of its log are the task's result.
"""

import asyncio
import json
import logging

LABEL = "kaji.systems/task"
LOG_TAIL = 200  # lines of the worker's log sent with the result
POLL_SECONDS = 5
DRY_RUN_CPU = "100m"
BINFMT_NAMES = {
    "arm64": "arm64",
    "arm": "arm",
    "riscv": "riscv64",
    "amd64": "amd64",
}  # the emulator's name for an architecture
log = logging.getLogger("kaji_agent")


def job_name(uid):
    return f"kaji-{uid[:13]}"


def job_for(uid, payload, namespace):
    name = job_name(uid)
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
        "restartPolicy": "Never",
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
    if payload.get("config"):
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
    def __init__(self, kube, send, poll=POLL_SECONDS):
        self.kube, self.send, self.poll = kube, send, poll  # send: async callable(message dict)
        self.active = set()

    def _jobs(self):
        return f"/apis/batch/v1/namespaces/{self.kube.namespace}/jobs"

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
            result["log"] = (
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
            while not (
                state := finished(
                    await asyncio.to_thread(self.kube.request, "GET", f"{self._jobs()}/{job_name(uid)}") or {}
                )
            ):
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
