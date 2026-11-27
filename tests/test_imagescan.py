"""The scan of the images that run in the cluster, by a Trivy Job each (imagescan.py), and what the agent sends of it."""
import asyncio
import json

import pytest
import websockets
from kaji_agent import imagescan
from kaji_agent.client import Agent

NS = "kaji-agent"
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
TRIVY = {
    "Metadata": {"OS": {"Family": "debian", "Name": "12.5"}},
    "Results": [
        {
            "Class": "os-pkgs",
            "Vulnerabilities": [
                {"VulnerabilityID": "CVE-2024-1", "PkgName": "libc6", "InstalledVersion": "2.36", "FixedVersion": "2.37", "Severity": "LOW", "Title": "t1", "PrimaryURL": "https://x/1"},
                {"VulnerabilityID": "CVE-2024-2", "PkgName": "openssl", "InstalledVersion": "3.0", "Severity": "CRITICAL", "Title": "t2"},
                {"VulnerabilityID": "CVE-2024-2", "PkgName": "openssl", "InstalledVersion": "3.0", "Severity": "CRITICAL", "Title": "again"},
                {"VulnerabilityID": "CVE-2024-3", "PkgName": "zlib", "InstalledVersion": "1", "Severity": "MEDIUM"},
            ],
        },
        {"Class": "lang-pkgs", "Vulnerabilities": None},
    ],
}


def log_of(report, noise="2024-01-01 WARN something\n"):
    return f"{noise}{imagescan.MARKER}\n{json.dumps(report)}\n"


class Kube:
    """A cluster that runs a Job when it is created and answers with a log."""

    namespace = NS

    def __init__(self, log=None, fail_create=False, never=False, pod_status=None):
        self.log, self.fail_create, self.never, self.pod_status = log, fail_create, never, pod_status or {}
        self.jobs, self.calls = {}, []

    def request(self, method, path, body=None, raw=False):
        self.calls.append((method, path))
        if method == "POST":
            if self.fail_create:
                raise OSError("forbidden")
            self.jobs[body["metadata"]["name"]] = body
            return body
        if method == "DELETE":
            self.jobs.pop(path.split("/jobs/")[1].split("?")[0], None)
            return {}
        if "/log" in path:
            return self.log
        if "/pods?" in path:
            return {"items": [{"metadata": {"name": "pod-1"}, "status": self.pod_status}]}
        return {"status": {} if self.never else {"succeeded": 1}}


def run(kube, **kw):
    return imagescan.scan(kube, "docker.io/library/nginx:1.27", DIGEST, sleep=lambda s: None, **kw)


def test_a_report_gives_the_os_and_the_findings_worst_first_each_once():
    found = run(Kube(log_of(TRIVY)))
    assert found["error"] == "" and found["os"] == "debian 12.5" and found["total"] == 3
    assert [(f["cve"], f["severity"]) for f in found["findings"]] == [
        ("CVE-2024-2", "high"),
        ("CVE-2024-3", "normal"),
        ("CVE-2024-1", "low"),
    ]
    assert found["findings"][2] == {"cve": "CVE-2024-1", "package": "libc6", "installed": "2.36", "fixed": "2.37", "severity": "low", "title": "t1", "url": "https://x/1"}
    assert found["image"] == "docker.io/library/nginx:1.27" and found["digest"] == DIGEST and found["scanned_at"].endswith("Z")


def test_a_clean_image_has_no_findings_and_no_error():
    found = run(Kube(log_of({"Metadata": {"OS": {"Family": "alpine", "Name": "3.20"}}, "Results": [{"Vulnerabilities": None}]})))
    assert found["error"] == "" and found["findings"] == [] and found["os"] == "alpine 3.20"


def test_the_findings_of_one_image_are_bounded(monkeypatch):
    monkeypatch.setattr(imagescan, "MAX_FINDINGS", 2)
    found = run(Kube(log_of(TRIVY)))
    assert found["total"] == 3 and len(found["findings"]) == 2


def test_a_scan_that_could_not_run_says_why_and_is_not_clean():
    found = run(Kube("FATAL unable to find the specified image\nFATAL Fatal error: scan error: pull access denied\n"))
    assert found["error"] == "FATAL Fatal error: scan error: pull access denied" and found["findings"] == []
    assert "not JSON" in run(Kube(f"{imagescan.MARKER}\n<html>"))["error"]
    assert run(Kube(""))["error"] == "Trivy printed nothing"


def test_a_job_that_cannot_be_made_or_does_not_end_is_an_error():
    assert "could not start the scan" in run(Kube(fail_create=True))["error"]
    ticks = iter(range(0, 10_000, 400))
    kube = Kube(never=True)
    got = imagescan.scan(kube, "a:1", DIGEST, deadline=10, sleep=lambda s: None, now=lambda: next(ticks))
    assert got["error"] == "the scan did not end in time"
    assert not kube.jobs  # whatever happened, the Job is removed


def test_a_trivy_image_the_cluster_cannot_pull_is_said_at_once_not_after_the_deadline():
    waiting = {"containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff", "message": "Back-off pulling image \"ghcr.io/x/trivy\""}}}]}
    kube = Kube(never=True, pod_status=waiting)
    ticks = iter(range(0, 10_000))
    got = imagescan.scan(kube, "a:1", DIGEST, trivy_image="ghcr.io/x/trivy:1", sleep=lambda s: None, now=lambda: next(ticks))
    assert got["error"].startswith("the cluster could not pull the Trivy image ghcr.io/x/trivy:1: Back-off pulling image")
    assert not kube.jobs  # the Job is removed
    first_try = {"containerStatuses": [{"state": {"waiting": {"reason": "ErrImagePull"}}}]}
    assert imagescan.pull_failure({"status": first_try}) == ""  # the first failure is not yet the answer


def test_the_job_runs_trivy_on_the_digest_as_a_plain_user_and_is_removed():
    kube = Kube(log_of(TRIVY))
    run(kube, trivy_image="registry.example/trivy:1", pull_secrets=[{"name": "pull"}])
    assert [m for m, _ in kube.calls][0] == "POST" and kube.calls[-1][0] == "DELETE" and not kube.jobs
    job = imagescan.scan_job(kube, "docker.io/library/nginx:1.27", DIGEST, "registry.example/trivy:1", [{"name": "pull"}])
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == "registry.example/trivy:1" and container["command"][-1] == f"docker.io/library/nginx@{DIGEST}"
    assert container["securityContext"]["runAsNonRoot"] and not container["securityContext"]["allowPrivilegeEscalation"]
    assert pod["imagePullSecrets"] == [{"name": "pull"}] and "nodeName" not in pod
    assert job["metadata"]["labels"] == {imagescan.LABEL: "scan"}  # not the label of a task: a restart would follow it


@pytest.mark.parametrize(
    "image,want",
    [
        ("nginx:1.27", f"nginx@{DIGEST}"),
        ("localhost:5000/team/app:v1", f"localhost:5000/team/app@{DIGEST}"),
        ("localhost:5000/app", f"localhost:5000/app@{DIGEST}"),
        (f"ghcr.io/o/i@{DIGEST}", f"ghcr.io/o/i@{DIGEST}"),
        (f"ghcr.io/o/i:v2@{DIGEST}", f"ghcr.io/o/i@{DIGEST}"),
    ],
)
def test_the_reference_is_the_name_and_the_digest(image, want):
    assert imagescan.reference(image, DIGEST) == want


def test_only_images_with_a_digest_not_scanned_lately_are_wanted():
    images = [{"image": "a:1", "digest": DIGEST}, {"image": "b:1", "digest": ""}, {"image": "a:2", "digest": DIGEST}, {"image": "c:1", "digest": OTHER}]
    assert imagescan.pending(images, {}, now=lambda: 0) == [("a:1", DIGEST), ("c:1", OTHER)]  # one scan for a digest, none without one
    done = {DIGEST: (0, True), OTHER: (0, False)}
    assert imagescan.pending(images, done, now=lambda: imagescan.RETRY_EVERY + 1) == [("c:1", OTHER)]  # a failure is tried again sooner
    assert imagescan.pending(images, done, now=lambda: imagescan.SCAN_EVERY + 1) == [("a:1", DIGEST), ("c:1", OTHER)]


class Cluster(Kube):
    """The pods list of the cluster report as well as the Jobs."""

    def request(self, method, path, body=None, raw=False):
        if path == "/api/v1/nodes":
            return {"items": [{"metadata": {"name": "n1"}, "status": {"nodeInfo": {"osImage": "Ubuntu"}}}]}
        if path.startswith("/api/v1/pods"):
            return {
                "items": [
                    {
                        "metadata": {"name": "web-1", "namespace": "default"},
                        "status": {"phase": "Running", "containerStatuses": [{"image": "nginx:1.27", "imageID": f"docker.io/library/nginx@{DIGEST}"}]},
                    }
                ]
            }
        if path == "/version":
            return {"gitVersion": "v1.31.1"}
        if "/pods/" in path and "/log" not in path:
            return None  # the agent's own pod is not readable: no pull secrets
        return super().request(method, path, body, raw)


async def test_the_agent_sends_the_cluster_and_then_the_scan_of_each_image(monkeypatch):
    seen = []
    monkeypatch.setattr(imagescan, "POLL", 0)

    async def gateway(ws):
        await ws.recv()  # hello
        await ws.send(json.dumps({"type": "welcome"}))
        while len(seen) < 2:
            seen.append(json.loads(await ws.recv()))
        await ws.close()

    server = await websockets.serve(gateway, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    agent = Agent(url, "kb_x", kube=Cluster(log_of(TRIVY)), interval=3600)
    async with websockets.connect(url, additional_headers={"Authorization": "Bearer kb_x"}) as ws:
        await asyncio.wait_for(agent.session(ws), 10)
    server.close()
    assert [(m["type"], m["kind"]) for m in seen] == [("report", "cluster"), ("report", "images")]
    scan = seen[1]["report"]["images"][0]
    assert scan["digest"] == DIGEST and scan["total"] == 3 and scan["error"] == ""
    assert agent.scanned[DIGEST][1] is True


async def test_no_job_is_started_when_the_user_switched_the_scan_off(monkeypatch):
    monkeypatch.setenv("KAJI_IMAGE_SCAN", "0")
    kube = Cluster(log_of(TRIVY))
    seen = []

    async def gateway(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "welcome"}))
        seen.append(json.loads(await ws.recv()))
        await asyncio.sleep(0.3)
        await ws.close()

    server = await websockets.serve(gateway, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    async with websockets.connect(url, additional_headers={"Authorization": "Bearer kb_x"}) as ws:
        await asyncio.wait_for(Agent(url, "kb_x", kube=kube, interval=3600).session(ws), 10)
    server.close()
    assert seen[0]["kind"] == "cluster" and not kube.jobs and not any(m == "POST" for m, _ in kube.calls)


def test_without_a_setting_the_public_trivy_is_used_and_a_blank_one_is_no_setting():
    assert imagescan.trivy_image({}) == imagescan.TRIVY_IMAGE == "ghcr.io/aquasecurity/trivy:0.58.1"
    assert imagescan.trivy_image({"KAJI_TRIVY_IMAGE": "  "}) == imagescan.TRIVY_IMAGE
    assert imagescan.trivy_image({"KAJI_TRIVY_IMAGE": " registry.corp/sec/trivy:1 "}) == "registry.corp/sec/trivy:1"
    assert imagescan.setting("KAJI_TRIVY_DB_REPOSITORY", {}) == ""


def test_the_job_uses_the_internal_image_and_database_when_they_are_set_and_the_public_ones_when_not():
    kube = Kube()
    plain = imagescan.scan_job(kube, "a:1", DIGEST, imagescan.trivy_image({}), [])
    env = plain["spec"]["template"]["spec"]["containers"][0]["env"]
    assert plain["spec"]["template"]["spec"]["containers"][0]["image"].startswith("ghcr.io/aquasecurity/trivy:")
    assert [e["name"] for e in env] == ["TRIVY_CACHE_DIR"]  # no database setting: Trivy's own default, the public one
    own = imagescan.scan_job(kube, "a:1", DIGEST, "registry.corp/sec/trivy:1", [{"name": "corp"}], "registry.corp/sec/trivy-db:2")
    container = own["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "registry.corp/sec/trivy:1" and {"name": "TRIVY_DB_REPOSITORY", "value": "registry.corp/sec/trivy-db:2"} in container["env"]
    assert own["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "corp"}]


def test_the_pull_secrets_are_the_agents_own(monkeypatch):
    monkeypatch.setattr(imagescan.nodeprobe, "own_pod", lambda kube: ("img", [{"name": "agent-pull"}]))
    assert imagescan.pull_secrets_of_agent(Kube()) == [{"name": "agent-pull"}]
    monkeypatch.setattr(imagescan.nodeprobe, "own_pod", lambda kube: None)
    assert imagescan.pull_secrets_of_agent(Kube()) == []


def test_the_database_repository_reaches_the_scan(monkeypatch):
    kube = Kube(log_of(TRIVY))
    imagescan.scan(kube, "a:1", DIGEST, "t:1", [], "registry.corp/db:2", sleep=lambda s: None)
    # the Job was made with it, and removed
    assert kube.calls[0][0] == "POST" and not kube.jobs
