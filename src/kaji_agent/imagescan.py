"""Scan the images that run in the cluster for known vulnerabilities, with Trivy.

For each image digest the agent starts a short Job in its own namespace that runs Trivy on the image (``trivy image``) and prints the JSON report after a marker line;
the agent reads it from the pod's log, keeps what the console shows (the CVE, the package, the installed and the fixed version, the severity) and removes the Job.
Only findings leave the cluster, never layers. The Job needs what the agent has: ``create`` and ``delete`` on jobs, ``list`` on pods and ``get`` on their logs.

Trivy fetches the image itself, so an image that needs credentials is reported as not scanned (the Job has none) and not as clean: a scan that could not run says
so. The Job also downloads Trivy's vulnerability database (``KAJI_TRIVY_IMAGE``'s default source, ghcr.io).
"""

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from . import nodeprobe

log = logging.getLogger("kaji_agent")

TRIVY_IMAGE = "ghcr.io/aquasecurity/trivy:0.58.1"
LABEL = "kaji.dev/probe"  # not the task label: a restarted agent follows the Jobs that carry that one (runner.LABEL)
MARKER = "KAJI_SCAN_REPORT"
DEADLINE = 900  # seconds for one image: the first Job on a node downloads the database
POLL = 5
SCAN_EVERY = 24 * 3600  # seconds before an image is scanned again: the database changes, the digest does not
RETRY_EVERY = 3600  # seconds before an image that could not be scanned is tried again
PARALLEL = 2  # Jobs at a time
MAX_FINDINGS = 500  # per image, worst first
SEVERITY = {"CRITICAL": "high", "HIGH": "high", "MEDIUM": "normal", "LOW": "low", "UNKNOWN": "low"}
RANK = {"high": 0, "normal": 1, "low": 2}
COMMAND = [
    "sh",
    "-c",
    f'trivy image --quiet --no-progress --scanners vuln --format json --output /tmp/report.json "$1" >&2'
    f' && echo {MARKER} && cat /tmp/report.json',
    "trivy",
]
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def job_name(digest):
    return "kaji-scan-" + hashlib.sha1(digest.encode()).hexdigest()[:10]


def reference(image, digest):
    """The image by its digest (``registry/name@sha256:...``): the tag may have moved since the pod started, the digest is what runs."""
    name = image.split("@")[0]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:  # a tag, not a registry's port (that is before the last slash)
        name = name[: len(name) - len(last)] + last.split(":")[0]
    return f"{name}@{digest}"


def scan_job(kube, image, digest, trivy_image, pull_secrets, db_repository=""):
    labels = {LABEL: "scan"}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name(digest), "namespace": kube.namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": DEADLINE,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "imagePullSecrets": pull_secrets,
                    "volumes": [{"name": "tmp", "emptyDir": {}}],
                    "containers": [
                        {
                            "name": "trivy",
                            "image": trivy_image,
                            "command": COMMAND + [reference(image, digest)],
                            "env": [{"name": "TRIVY_CACHE_DIR", "value": "/tmp/trivy"}]
                            + ([{"name": "TRIVY_DB_REPOSITORY", "value": db_repository}] if db_repository else []),
                            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "2Gi"}},
                            "securityContext": {
                                "runAsUser": 65534,
                                "runAsNonRoot": True,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }


def _text(value, size):
    return " ".join(value.split())[:size] if isinstance(value, str) else ""


def parse(log_text):
    """``(os, total, findings)`` from the Job's log, or raise ``ValueError`` with why there is no report. ``findings`` are worst first and at most ``MAX_FINDINGS``."""
    if MARKER not in log_text:
        lines = [ln for ln in log_text.strip().splitlines() if ln.strip()]
        raise ValueError(_text(lines[-1], 300) if lines else "Trivy printed nothing")
    try:
        data = json.loads(log_text.split(MARKER, 1)[1])
    except ValueError:
        raise ValueError("Trivy's report is not JSON") from None
    return report_of(data)


def report_of(data):
    """``(os, total, findings)`` of Trivy's JSON report (a dict): the OS it recognised (``debian 12.5``, empty if none), how many findings there are, and the first ``MAX_FINDINGS``, worst first."""
    os_info = ((data.get("Metadata") or {}).get("OS") or {}) if isinstance(data, dict) else {}
    found = {}
    for result in (data.get("Results") or []) if isinstance(data, dict) else []:
        for v in result.get("Vulnerabilities") or []:
            if not (isinstance(v, dict) and v.get("VulnerabilityID") and v.get("PkgName")):
                continue
            key = (v["VulnerabilityID"], v["PkgName"], v.get("InstalledVersion") or "")
            found[key] = {
                "cve": _text(v["VulnerabilityID"], 40),
                "package": _text(v["PkgName"], 200),
                "installed": _text(v.get("InstalledVersion"), 100),
                "fixed": _text(v.get("FixedVersion"), 100),
                "severity": SEVERITY.get(str(v.get("Severity")).upper(), "low"),
                "title": _text(v.get("Title"), 300),
                "url": _text(v.get("PrimaryURL"), 300),
            }
    findings = sorted(found.values(), key=lambda f: (RANK[f["severity"]], f["package"], f["cve"]))
    os_name = " ".join(x for x in (os_info.get("Family"), os_info.get("Name")) if x)
    return _text(os_name, 60), len(findings), findings[:MAX_FINDINGS]


PULL_FAILURES = ("ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull")  # the pod cannot start: ErrImagePull alone is the first try, it may still work


def pull_failure(pod):
    """Why the Trivy pod cannot start, from the waiting state of its container (``ImagePullBackOff``), or ``""`` while it can."""
    for c in ((pod.get("status") or {}).get("containerStatuses") or []):
        waiting = (c.get("state") or {}).get("waiting") or {}
        if waiting.get("reason") in PULL_FAILURES:
            return _text(waiting.get("message") or waiting["reason"], 200)
    return ""


def _result(image, digest, **fields):
    return {
        "image": image,
        "digest": digest,
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "os": "",
        "total": 0,
        "findings": [],
        "error": "",
        **fields,
    }


def scan(kube, image, digest, trivy_image=TRIVY_IMAGE, pull_secrets=(), db_repository="", deadline=DEADLINE, poll=POLL, now=time.monotonic, sleep=time.sleep):
    """The scan of one image as a dict (``image``, ``digest``, ``scanned_at``, ``os``, ``total``, ``findings``, ``error``). ``error`` says why the image was not scanned; the findings
    are then empty, which is not "clean". Blocking: the agent calls it in a thread."""
    base = f"/apis/batch/v1/namespaces/{kube.namespace}/jobs"
    name = job_name(digest)
    try:
        kube.request("POST", base, scan_job(kube, image, digest, trivy_image, list(pull_secrets), db_repository))
    except OSError as err:
        return _result(image, digest, error=_text(f"could not start the scan: {err}", 300))
    try:
        end = now() + deadline + 60
        status = {}
        while now() < end:
            status = (kube.request("GET", f"{base}/{name}") or {}).get("status") or {}
            if status.get("succeeded") or status.get("failed"):
                break
            for pod in (kube.request("GET", f"/api/v1/namespaces/{kube.namespace}/pods?labelSelector=job-name%3D{name}") or {}).get("items", []):
                if why := pull_failure(pod):  # no Trivy, so no scan: say so now, not after the deadline
                    return _result(image, digest, error=_text(f"the cluster could not pull the Trivy image {trivy_image}: {why}", 300))
            sleep(poll)
        else:
            return _result(image, digest, error="the scan did not end in time")
        pods = (kube.request("GET", f"/api/v1/namespaces/{kube.namespace}/pods?labelSelector=job-name%3D{name}") or {}).get("items", [])
        text = ""
        if pods:
            text = kube.request("GET", f"/api/v1/namespaces/{kube.namespace}/pods/{pods[0]['metadata']['name']}/log", raw=True) or ""
        try:
            os_name, total, findings = parse(text)
        except ValueError as err:
            return _result(image, digest, error=_text(str(err), 300))
        return _result(image, digest, os=os_name, total=total, findings=findings)
    except OSError as err:
        return _result(image, digest, error=_text(f"could not read the scan: {err}", 300))
    finally:
        try:
            kube.request("DELETE", f"{base}/{name}?propagationPolicy=Background")
        except OSError:
            pass


def pending(images, done, now=time.monotonic):
    """The ``(image, digest)`` that need a scan: the digest is known and valid, and it was not scanned lately (``done``: ``{digest: (when, ok)}``)."""
    wanted, seen = [], set()
    for entry in images:
        digest = entry.get("digest") or ""
        if not DIGEST.match(digest) or digest in seen:
            continue
        seen.add(digest)
        last = done.get(digest)
        if last is None or now() - last[0] >= (SCAN_EVERY if last[1] else RETRY_EVERY):
            wanted.append((entry["image"], digest))
    return wanted


def setting(name, environ=os.environ):
    """A setting of the agent, stripped; empty when it is unset or blank, which means the public default."""
    return (environ.get(name) or "").strip()


def trivy_image(environ=os.environ):
    """The Trivy image the Jobs run: ``KAJI_TRIVY_IMAGE`` (an internal registry's), else the public one."""
    return setting("KAJI_TRIVY_IMAGE", environ) or TRIVY_IMAGE


def pull_secrets_of_agent(kube):
    """The pull secrets of the agent's own pod (the chart's ``imagePullSecrets``), for a Trivy image kept in a private registry; empty when they cannot be read. The Job's pod also gets
    those of its namespace's service account, as any pod does."""
    found = nodeprobe.own_pod(kube)
    return found[1] if found else []
