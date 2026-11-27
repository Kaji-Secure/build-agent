"""Scan the host's own packages for known vulnerabilities, with Trivy, which the agent fetches itself if the machine has none.

``trivy rootfs`` reads the host's package databases (dpkg, rpm, apk ...) and language lock files and matches them with Trivy's advisories. Nothing has to be installed
first: the agent uses ``KAJI_TRIVY_BIN``, else a ``trivy`` on the ``PATH``, else the copy it keeps in its state directory, else it **downloads a pinned release** from GitHub
(``KAJI_TRIVY_URL`` names a mirror of the release directory), checks the archive against the release's checksum file, and keeps only the ``trivy`` binary. Only findings leave
the machine.

* A Gentoo host is not scanned: Trivy has no Gentoo advisories, and the host's advisories come from GLSA (:doc:`hosts`). It is said, not hidden.
* An agent in a container sees the container's files, not the host's: it is scanned only when the host's root is mounted (``KAJI_HOST_ROOT``, read-only).
* An organization that keeps Trivy in its own repositories sets ``KAJI_TRIVY_URL`` (a directory laid out like the release), ``KAJI_TRIVY_BIN`` or ``KAJI_TRIVY_DB_REPOSITORY``
  in the agent's settings file; a setting that is unset or blank means the public one. Trivy's own ``TRIVY_USERNAME`` and ``TRIVY_PASSWORD`` in that file reach the scan.
* Whatever goes wrong is an ``error``: the findings are then empty, which is not "clean".
"""

import hashlib
import io
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import enroll, imagescan

log = logging.getLogger("kaji_agent")

TRIVY_VERSION = "0.58.1"
RELEASE = "https://github.com/aquasecurity/trivy/releases/download/v{version}"
ASSETS = {"x86_64": "Linux-64bit", "aarch64": "Linux-ARM64"}  # platform.machine() -> the release's name for it
MAX_DOWNLOAD = 200 * 1024 * 1024  # bytes: the archive is about 50 MB
SCAN_EVERY = imagescan.SCAN_EVERY
RETRY_EVERY = imagescan.RETRY_EVERY
TIMEOUT = 1800  # seconds for one scan: the first one downloads the database
SKIP_DIRS = "/proc,/sys,/dev,/run,/var/lib/docker,/var/lib/containerd,/var/lib/containers"  # not the host's packages: pseudo files, and the images and layers of containers
OS_ID = re.compile(r'^ID=(?:"([^"]*)"|(\S*))\s*$', re.MULTILINE)


class ScanError(Exception):
    """Why the host could not be scanned: the text is what the console shows."""


def host_root(environ=os.environ):
    return Path(environ.get("KAJI_HOST_ROOT") or "/")


def in_container(root=Path("/")):
    return (root / ".dockerenv").exists() or (root / "run/.containerenv").exists()


def os_id(root):
    """``ID`` of the host's ``/etc/os-release`` (``debian``, ``gentoo``), or ``""``."""
    for name in ("etc/os-release", "usr/lib/os-release"):
        try:
            m = OS_ID.search((root / name).read_text())
        except OSError:
            continue
        if m:
            return (m.group(1) or m.group(2) or "").lower()
    return ""


def cache_dir(environ=os.environ):
    return Path(environ.get("KAJI_CACHE_DIR") or enroll.state_dir() / "cache")


def asset(machine=None):
    name = ASSETS.get(machine or platform.machine())
    if not name:
        raise ScanError(f"no Trivy release for {machine or platform.machine()}: set KAJI_TRIVY_BIN to one")
    return f"trivy_{TRIVY_VERSION}_{name}.tar.gz"


def _fetch(url, limit=MAX_DOWNLOAD):
    with urllib.request.urlopen(url, timeout=120) as resp:  # the pinned release, or the mirror the user named
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ScanError(f"{url} is bigger than {limit} bytes")
    return data


def download(directory, fetch=_fetch, environ=os.environ, machine=None):
    """Download Trivy into ``directory`` and return the path of the binary. The archive must match the sha256 the release's checksum file gives for it; only a member named exactly
    ``trivy`` is taken, and it is written under a temporary name and moved into place, so a cut download leaves nothing that could be run."""
    base = (imagescan.setting("KAJI_TRIVY_URL", environ) or RELEASE.format(version=TRIVY_VERSION)).rstrip("/")
    name = asset(machine)
    try:
        archive = fetch(f"{base}/{name}")
        sums = fetch(f"{base}/trivy_{TRIVY_VERSION}_checksums.txt").decode(errors="replace")
    except (OSError, ValueError) as err:
        raise ScanError(f"could not download Trivy from {base}: {err}") from err
    want = next((ln.split()[0] for ln in sums.splitlines() if ln.split()[1:] == [name]), "")
    if not want or hashlib.sha256(archive).hexdigest() != want.lower():
        raise ScanError(f"the checksum of {name} does not match the release's: it was not used")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            member = next((m for m in tar.getmembers() if m.name == "trivy" and m.isfile()), None)
            if member is None:
                raise ScanError(f"{name} has no trivy in it")
            source = tar.extractfile(member)
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"trivy-{TRIVY_VERSION}"
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".trivy-", delete=False) as out:
                shutil.copyfileobj(source, out)
            os.chmod(out.name, 0o755)
            os.replace(out.name, target)
    except (tarfile.TarError, OSError) as err:
        raise ScanError(f"could not unpack Trivy: {err}") from err
    return target


def find_trivy(directory, environ=os.environ, which=shutil.which, fetch=_fetch, machine=None):
    """The Trivy to run: ``KAJI_TRIVY_BIN``, one on the ``PATH``, the copy kept from an earlier download, or a new download."""
    if imagescan.setting("KAJI_TRIVY_BIN", environ):
        return Path(imagescan.setting("KAJI_TRIVY_BIN", environ))
    if found := which("trivy"):
        return Path(found)
    kept = Path(directory) / f"trivy-{TRIVY_VERSION}"
    if kept.is_file() and os.access(kept, os.X_OK):
        return kept
    return download(directory, fetch=fetch, environ=environ, machine=machine)


def _result(**fields):
    return {
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "os": "",
        "total": 0,
        "findings": [],
        "error": "",
        **fields,
    }


def scan(directory=None, environ=os.environ, run=subprocess.run, **kw):
    """The scan of this host as a dict (``scanned_at``, ``os``, ``total``, ``findings``, ``error``). Blocking: the agent calls it in a thread. ``kw`` goes to :func:`find_trivy`."""
    root = host_root(environ)
    directory = Path(directory) if directory else cache_dir(environ)
    try:
        if not environ.get("KAJI_HOST_ROOT") and in_container():
            raise ScanError("the agent runs in a container and does not see the host's files: mount the host's root read-only and set KAJI_HOST_ROOT")
        if os_id(root) == "gentoo":
            raise ScanError("Gentoo: Trivy has no Gentoo advisories, the advisories above come from GLSA")
        trivy = find_trivy(directory, environ=environ, **kw)
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as tmp:
            out = Path(tmp) / "report.json"
            cmd = [
                str(trivy), "rootfs", "--quiet", "--no-progress", "--scanners", "vuln", "--format", "json",
                "--cache-dir", str(directory / "trivy-db"), "--skip-dirs", ",".join(str(root / d.lstrip("/")) for d in SKIP_DIRS.split(",")),
                "--output", str(out), str(root),
            ]  # fmt: skip
            env = {**os.environ, **({"TRIVY_DB_REPOSITORY": db} if (db := imagescan.setting("KAJI_TRIVY_DB_REPOSITORY", environ)) else {})}
            try:
                done = run(cmd, capture_output=True, text=True, timeout=TIMEOUT, env=env)
            except (OSError, subprocess.TimeoutExpired) as err:
                raise ScanError(f"Trivy did not run: {err}") from err
            if done.returncode != 0 or not out.exists():
                lines = [ln for ln in (done.stderr or "").strip().splitlines() if ln.strip()]
                raise ScanError(imagescan._text(lines[-1], 300) if lines else f"Trivy failed ({done.returncode})")
            try:
                data = json.loads(out.read_text())
            except ValueError:
                raise ScanError("Trivy's report is not JSON") from None
        os_name, total, findings = imagescan.report_of(data)
        return _result(os=os_name, total=total, findings=findings)
    except ScanError as err:
        return _result(error=imagescan._text(str(err), 300))


def due(last, now):
    """Whether to scan: never scanned, or the last scan (``(when, worked)``) is older than a day, or than an hour if it failed."""
    return last is None or now - last[0] >= (SCAN_EVERY if last[1] else RETRY_EVERY)
