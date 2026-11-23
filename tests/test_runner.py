import json

from kaji_agent.runner import LABEL, Runner, extras_for, finished, job_for, job_name

UID = "0b7a3c9e-1111-2222-3333-444455556666"
PAYLOAD = {
    "image": "w:1",
    "command": ["waldmeister", "build"],
    "env": {"WM_TASK": "{}"},
    "cpus": 3,
    "memory": "4Gi",
    "arch": "arm64",
}


class FakeKube:
    namespace = "kaji"

    def __init__(self, ends="done", message=None):
        self.made = []
        self.jobs, self.calls, self.polls, self.ends, self.message = {}, [], 0, ends, message

    def request(self, method, path, body=None, raw=False):
        self.calls.append((method, path))
        if "/log" in path:
            return "last line\n"
        if "/pods" in path:
            term = {"message": json.dumps(self.message or {"reused": False, "copied": 2}), "reason": "Completed"}
            return {
                "items": [
                    {"metadata": {"name": "p"}, "status": {"containerStatuses": [{"state": {"terminated": term}}]}}
                ]
            }
        if method == "POST" and "/jobs" not in path:
            self.made.append((path, body))
            return body
        if method == "POST":
            body["metadata"]["uid"] = "job-uid"
            self.jobs[body["metadata"]["name"]] = body
            return body
        name = path.split("/jobs")[1].lstrip("/").split("?")[0]
        if not name:
            return {"items": list(self.jobs.values())}
        job = self.jobs.get(name)
        if job is None:
            return None
        self.polls += 1
        return {**job, "status": {self.ends == "done" and "succeeded" or "failed": 1} if self.polls > 1 else {}}


async def sent(kube):
    out = []

    async def send(message):
        out.append(message)

    await Runner(kube, send, poll=0).run(UID, PAYLOAD)
    return out


def test_the_job_has_the_workers_image_command_environment_and_resources():
    job = job_for(UID, PAYLOAD, "kaji")
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert job["metadata"]["name"] == job_name(UID) and job["metadata"]["labels"] == {LABEL: UID}
    assert (
        c["image"] == "w:1"
        and c["command"] == ["waldmeister", "build"]
        and c["env"] == [{"name": "WM_TASK", "value": "{}"}]
    )
    assert c["resources"] == {"requests": {"cpu": "3", "memory": "4Gi"}, "limits": {"cpu": "3", "memory": "4Gi"}}
    assert pod["nodeSelector"] == {"kubernetes.io/arch": "arm64"} and job["spec"]["backoffLimit"] == 0


def test_a_dry_run_reserves_little():
    c = job_for(UID, {**PAYLOAD, "dry_run": True}, "kaji")["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"] == {"requests": {"cpu": "100m"}, "limits": {"cpu": "3"}}


def test_finished():
    assert (
        finished({"status": {"succeeded": 1}}) == "done"
        and finished({"status": {"failed": 1}}) == "failed"
        and finished({}) is None
    )


async def test_a_task_runs_and_its_result_is_what_the_worker_said_with_the_log():
    kube = FakeKube()
    out = await sent(kube)
    assert out[0] == {"type": "status", "task": UID, "state": "running"}
    assert out[-1]["state"] == "done" and out[-1]["result"] == {"reused": False, "copied": 2, "log": "last line\n"}
    assert [m for m, _ in kube.calls].count("POST") == 1


async def test_a_failed_job_reports_failed():
    out = await sent(FakeKube(ends="failed", message={"message": "emerge failed"}))
    assert out[-1]["state"] == "failed" and out[-1]["result"]["message"] == "emerge failed"


async def test_a_job_that_cannot_be_made_is_reported_failed():
    class Refusing(FakeKube):
        def request(self, method, path, body=None, raw=False):
            if method == "POST":
                raise OSError("forbidden")
            return super().request(method, path, body, raw)

    out = await sent(Refusing())
    assert out[-1]["state"] == "failed" and "forbidden" in out[-1]["result"]["message"]


async def test_a_job_that_exists_is_followed_not_made_again():
    kube = FakeKube()
    kube.jobs[job_name(UID)] = job_for(UID, PAYLOAD, "kaji")
    await sent(kube)
    assert "POST" not in [m for m, _ in kube.calls]


async def test_the_same_task_is_not_followed_twice():
    runner = Runner(FakeKube(), None)
    runner.active.add(UID)
    await runner.run(UID, PAYLOAD)  # no send: it would fail if it did anything


FOREIGN = {
    **PAYLOAD,
    "tree_image": "reg.example/trees/gentoo:abc",
    "tree_mount": "/var/db/repos/gentoo",
    "config": {"files.json": "{}"},
    "pull_secret": '{"auths": {}}',
    "register_binfmt": True,
    "binfmt_image": "binfmt:1",
}


def test_the_job_gets_the_tree_the_configuration_and_the_pull_login():
    pod = job_for(UID, FOREIGN, "kaji")["spec"]["template"]["spec"]
    mounts = {m["name"]: m for m in pod["containers"][0]["volumeMounts"]}
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert mounts["tree"] == {"name": "tree", "mountPath": "/var/db/repos/gentoo", "readOnly": True}
    assert volumes["tree"]["image"]["reference"] == "reg.example/trees/gentoo:abc"
    assert (
        volumes["config"]["configMap"]["name"] == f"{job_name(UID)}-config"
        and mounts["config"]["mountPath"] == "/wm/config"
    )
    assert pod["imagePullSecrets"] == [{"name": f"{job_name(UID)}-pull"}]
    assert pod["initContainers"][0]["securityContext"] == {"privileged": True} and pod["initContainers"][0]["args"] == [
        "--install",
        "arm64",
    ]


def test_a_job_without_those_asks_for_none_of_them():
    pod = job_for(UID, PAYLOAD, "kaji")["spec"]["template"]["spec"]
    assert (
        [v["name"] for v in pod["volumes"]] == ["distfiles"]
        and "imagePullSecrets" not in pod
        and "initContainers" not in pod
    )


def test_the_configmap_and_the_secret_are_owned_by_the_job():
    job = {**job_for(UID, FOREIGN, "kaji"), "metadata": {**job_for(UID, FOREIGN, "kaji")["metadata"], "uid": "u1"}}
    (cm_path, cm), (sec_path, sec) = extras_for(UID, FOREIGN, "kaji", job)
    assert (
        cm_path.endswith("/configmaps")
        and cm["data"] == {"files.json": "{}"}
        and sec["type"] == "kubernetes.io/dockerconfigjson"
    )
    assert sec["stringData"] == {".dockerconfigjson": '{"auths": {}}'}
    assert cm["metadata"]["ownerReferences"][0]["uid"] == "u1" == sec["metadata"]["ownerReferences"][0]["uid"]


async def test_the_runner_makes_the_job_and_then_what_it_needs():
    kube = FakeKube()
    out = []

    async def send(message):
        out.append(message)

    await Runner(kube, send, poll=0).run(UID, FOREIGN)
    assert [p.rsplit("/", 1)[1] for p, _ in kube.made] == ["configmaps", "secrets"] and out[-1]["state"] == "done"


ARTIFACT = {
    "kind": "artifact-build",
    "image": "byon.example/worker/waldmeister-builder:1",
    "command": ["waldmeister", "rootfs-build"],
    "env": {"WM_ROOTFS": "{}"},
    "resources": {"requests": {"cpu": "2", "memory": "4Gi"}, "limits": {"memory": "4Gi"}},
    "rootless": True,
    "arch": "",
    "pull_secret": "",
    "volumes": [
        {"name": "store", "kind": "empty", "path": "/home/podman/.local/share/containers", "size": "10Gi"},
        {
            "name": "tree",
            "kind": "image",
            "path": "/var/db/repos/gentoo",
            "read_only": True,
            "image": "byon.example/trees/acme/acme-t:abc",
        },
        {"name": "config", "kind": "config", "path": "/wm/config", "read_only": True, "data": {"files.json": "{}"}},
        {"name": "auth", "kind": "secret", "path": "/auth", "read_only": True, "data": {"config.json": "{}"}},
    ],
}


def test_a_task_that_describes_its_pod_gets_it_as_described():
    job = job_for(UID, ARTIFACT, "kaji")
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    name = job_name(UID)
    assert c["resources"] == ARTIFACT["resources"] and c["command"] == ["waldmeister", "rootfs-build"]
    assert (
        c["securityContext"]["seccompProfile"] == {"type": "Unconfined"} and c["securityContext"]["runAsUser"] == 1000
    )
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["store"] == {"name": "store", "emptyDir": {"sizeLimit": "10Gi"}}
    assert vols["tree"]["image"]["reference"] == "byon.example/trees/acme/acme-t:abc"
    assert (
        vols["config"]["configMap"]["name"] == f"{name}-config"
        and vols["auth"]["secret"]["secretName"] == f"{name}-auth"
    )
    mounts = {m["name"]: m for m in c["volumeMounts"]}
    assert (
        mounts["tree"] == {"name": "tree", "mountPath": "/var/db/repos/gentoo", "readOnly": True}
        and "readOnly" not in mounts["store"]
    )


def test_a_task_without_rootless_has_no_security_context():
    task = {**ARTIFACT, "rootless": False}
    assert "securityContext" not in job_for(UID, task, "kaji")["spec"]["template"]["spec"]["containers"][0]


def test_the_configmap_and_the_secret_of_a_described_pod_are_made_and_owned_by_the_job():
    job = {**job_for(UID, ARTIFACT, "kaji"), "metadata": {**job_for(UID, ARTIFACT, "kaji")["metadata"], "uid": "u1"}}
    made = {obj["kind"]: obj for _, obj in extras_for(UID, ARTIFACT, "kaji", job)}
    assert made["ConfigMap"]["data"] == {"files.json": "{}"} and made["Secret"]["stringData"] == {"config.json": "{}"}
    assert (
        made["Secret"]["metadata"]["name"] == f"{job_name(UID)}-auth"
        and made["Secret"]["metadata"]["ownerReferences"][0]["uid"] == "u1"
    )
    assert len(made) == 2


def test_an_unknown_kind_of_volume_is_refused():
    import pytest

    with pytest.raises(ValueError):
        job_for(UID, {**ARTIFACT, "volumes": [{"name": "x", "kind": "hostPath", "path": "/"}]}, "kaji")


def test_a_tasks_token_is_taken_out_of_its_log():
    from kaji_agent.runner import redact

    token = "kt1.1795457781." + "ab12" * 16
    log = f"pap: fetching index from https://kaji-task:{token}@repo.example/Packages\nand again {token}\nnothing else is touched: kt1.x"
    out = redact(log)
    assert token not in out and "https://kaji-task:kt1.***@repo.example/Packages" in out and out.endswith("kt1.x")


async def test_the_log_a_runner_reports_has_no_token():
    token = "kt1.1795457781." + "cd34" * 16

    class Leaky(FakeKube):
        def request(self, method, path, body=None, raw=False):
            return (
                f"fetching https://kaji-task:{token}@h/Packages\n"
                if "/log" in path
                else super().request(method, path, body, raw)
            )

    out = await sent(Leaky())
    assert token not in json.dumps(out) and "kt1.***" in out[-1]["result"]["log"]
