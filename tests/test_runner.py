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
