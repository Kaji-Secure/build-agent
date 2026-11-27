"""The scan of the host's own packages with Trivy, which the agent fetches if the machine has none (hostscan.py)."""
import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import websockets
from kaji_agent import hostscan, imagescan
from kaji_agent.client import Agent

TRIVY = {
    "Metadata": {"OS": {"Family": "debian", "Name": "12.5"}},
    "Results": [
        {"Vulnerabilities": [{"VulnerabilityID": "CVE-2024-2", "PkgName": "openssl", "InstalledVersion": "3.0", "FixedVersion": "3.0.1", "Severity": "HIGH"}]}
    ],
}
NAME = f"trivy_{hostscan.TRIVY_VERSION}_Linux-64bit.tar.gz"


def archive(members=(("trivy", b"#!/bin/sh\necho trivy\n"),)):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def release(data, listed=NAME, digest=None):
    """What the release directory serves: the archive, and a checksum file that lists ``listed``."""
    sums = f"{digest or hashlib.sha256(data).hexdigest()}  {listed}\n{'0' * 64}  other.tar.gz\n"
    files = {f"https://mirror/{NAME}": data, f"https://mirror/trivy_{hostscan.TRIVY_VERSION}_checksums.txt": sums.encode()}

    def fetch(url, limit=None):
        if url not in files:
            raise OSError(f"404 {url}")
        return files[url]

    return fetch


ENV = {"KAJI_TRIVY_URL": "https://mirror/"}


def test_trivy_is_downloaded_checked_and_only_the_binary_is_kept(tmp_path):
    got = hostscan.download(tmp_path / "cache", fetch=release(archive()), environ=ENV, machine="x86_64")
    assert got == tmp_path / "cache" / f"trivy-{hostscan.TRIVY_VERSION}" and got.stat().st_mode & 0o111
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [got.name]  # nothing else is left there


def test_an_archive_that_does_not_match_its_checksum_is_not_used(tmp_path):
    with pytest.raises(hostscan.ScanError, match="checksum"):
        hostscan.download(tmp_path, fetch=release(archive(), digest="1" * 64), environ=ENV, machine="x86_64")
    assert not list(tmp_path.iterdir())


def test_a_release_with_no_checksum_for_the_archive_is_not_used(tmp_path):
    with pytest.raises(hostscan.ScanError, match="checksum"):
        hostscan.download(tmp_path, fetch=release(archive(), listed="other.tar.gz"), environ=ENV, machine="x86_64")


def test_only_a_member_named_trivy_is_taken(tmp_path):
    with pytest.raises(hostscan.ScanError, match="no trivy"):
        hostscan.download(tmp_path, fetch=release(archive([("../trivy", b"x"), ("bin/trivy", b"x")])), environ=ENV, machine="x86_64")


def test_a_download_that_fails_or_a_machine_with_no_release_is_said(tmp_path):
    with pytest.raises(hostscan.ScanError, match="could not download Trivy"):
        hostscan.download(tmp_path, fetch=lambda url, limit=None: (_ for _ in ()).throw(OSError("offline")), environ=ENV, machine="x86_64")
    with pytest.raises(hostscan.ScanError, match="no Trivy release for riscv64"):
        hostscan.download(tmp_path, fetch=release(b""), environ=ENV, machine="riscv64")


def test_the_trivy_to_run_is_the_one_named_then_the_one_on_the_path_then_the_kept_one_then_a_download(tmp_path):
    never = lambda url, limit=None: pytest.fail("nothing is downloaded")  # noqa: E731
    assert hostscan.find_trivy(tmp_path, environ={"KAJI_TRIVY_BIN": "/opt/trivy"}, which=lambda n: "/usr/bin/trivy", fetch=never) == Path("/opt/trivy")
    assert hostscan.find_trivy(tmp_path, environ={}, which=lambda n: "/usr/bin/trivy", fetch=never) == Path("/usr/bin/trivy")
    kept = hostscan.find_trivy(tmp_path, environ=ENV, which=lambda n: None, fetch=release(archive()), machine="x86_64")
    assert kept.parent == tmp_path
    assert hostscan.find_trivy(tmp_path, environ=ENV, which=lambda n: None, fetch=never) == kept  # the second time it is there


def root(tmp_path, os_id="debian"):
    (tmp_path / "etc").mkdir(parents=True, exist_ok=True)
    (tmp_path / "etc/os-release").write_text(f'NAME="X"\nID={os_id}\nVERSION_ID="12"\n')
    return tmp_path


def fake_run(report=TRIVY, code=0, stderr=""):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if report is not None:
            Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps(report))
        return SimpleNamespace(returncode=code, stderr=stderr)

    run.calls = calls
    return run


def scan(tmp_path, env=None, **kw):
    state = tmp_path / "state"
    env = {"KAJI_HOST_ROOT": str(root(tmp_path / "host")), "KAJI_TRIVY_BIN": "/usr/bin/trivy", **(env or {})}
    return hostscan.scan(state, environ=env, **kw)


def test_the_host_is_scanned_by_its_root_and_the_findings_are_kept(tmp_path):
    run = fake_run()
    got = scan(tmp_path, run=run)
    assert got["error"] == "" and got["os"] == "debian 12.5" and got["total"] == 1 and got["findings"][0]["severity"] == "high"
    cmd = run.calls[0]
    assert cmd[:3] == ["/usr/bin/trivy", "rootfs", "--quiet"] and cmd[-1] == str(tmp_path / "host")
    assert str(tmp_path / "host/proc") in cmd[cmd.index("--skip-dirs") + 1].split(",")  # the pseudo files of the host, not of the agent
    assert str(tmp_path / "state/trivy-db") == cmd[cmd.index("--cache-dir") + 1]


def test_a_gentoo_host_is_not_scanned_and_says_why(tmp_path):
    run = fake_run()
    got = scan(tmp_path, run=run, env={"KAJI_HOST_ROOT": str(root(tmp_path / "g", "gentoo"))})
    assert "Gentoo" in got["error"] and "GLSA" in got["error"] and not run.calls


def test_an_agent_in_a_container_does_not_scan_the_containers_files_as_if_they_were_the_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(hostscan, "in_container", lambda root=None: True)
    run = fake_run()
    got = hostscan.scan(tmp_path, environ={"KAJI_TRIVY_BIN": "/usr/bin/trivy"}, run=run)
    assert "KAJI_HOST_ROOT" in got["error"] and not run.calls


def test_a_scan_that_fails_says_why_and_is_not_clean(tmp_path):
    assert scan(tmp_path, run=fake_run(None, 1, "x\nFATAL db download failed\n"))["error"] == "FATAL db download failed"
    assert "not JSON" in scan(tmp_path, run=lambda cmd, **kw: (Path(cmd[cmd.index("--output") + 1]).write_text("<x"), SimpleNamespace(returncode=0, stderr=""))[1])["error"]

    def boom(cmd, **kw):
        raise OSError("exec format error")

    assert "Trivy did not run" in scan(tmp_path, run=boom)["error"]


def test_a_download_that_fails_is_a_scan_that_says_so(tmp_path):
    got = hostscan.scan(tmp_path / "s", environ={"KAJI_HOST_ROOT": str(root(tmp_path / "h")), **ENV}, which=lambda n: None, fetch=lambda u, limit=None: (_ for _ in ()).throw(OSError("offline")), run=fake_run(), machine="x86_64")
    assert got["error"].startswith("could not download Trivy") and got["findings"] == []


def test_a_host_is_due_when_never_scanned_after_a_day_or_after_an_hour_if_it_failed():
    assert hostscan.due(None, 0)
    assert not hostscan.due((0, True), hostscan.RETRY_EVERY + 1) and hostscan.due((0, True), hostscan.SCAN_EVERY)
    assert hostscan.due((0, False), hostscan.RETRY_EVERY)


async def test_a_host_agent_sends_its_report_and_then_its_scan(monkeypatch):
    seen = []
    monkeypatch.setattr(hostscan, "scan", lambda *a, **k: {"scanned_at": "2026-11-27T10:00:00Z", "os": "debian 12", "total": 0, "findings": [], "error": ""})
    from kaji_agent import hostinfo

    monkeypatch.setattr(hostinfo, "report", lambda: {"hostname": "h"})

    async def gateway(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "welcome"}))
        while len(seen) < 2:
            seen.append(json.loads(await ws.recv()))
        await ws.close()

    server = await websockets.serve(gateway, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    agent = Agent(url, "kb_x", role="host", interval=3600)
    async with websockets.connect(url, additional_headers={"Authorization": "Bearer kb_x"}) as ws:
        await asyncio.wait_for(agent.session(ws), 10)
    server.close()
    assert [(m["type"], m["kind"]) for m in seen] == [("report", "host"), ("report", "hostscan")]
    assert agent.host_scanned[1] is True and imagescan  # a scan that worked is not repeated at the next report


async def test_the_scan_is_not_started_when_switched_off(monkeypatch):
    monkeypatch.setenv("KAJI_HOST_SCAN", "0")
    monkeypatch.setattr(hostscan, "scan", lambda *a, **k: pytest.fail("no scan"))
    from kaji_agent import hostinfo

    monkeypatch.setattr(hostinfo, "report", lambda: {"hostname": "h"})
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
        await asyncio.wait_for(Agent(url, "kb_x", role="host", interval=3600).session(ws), 10)
    server.close()
    assert seen[0]["kind"] == "host"


def test_without_a_setting_the_release_comes_from_github_and_a_blank_setting_is_none(tmp_path):
    urls = []

    def fetch(url, limit=None):
        urls.append(url)
        raise OSError("offline")

    for env in ({}, {"KAJI_TRIVY_URL": "  "}):
        with pytest.raises(hostscan.ScanError):
            hostscan.download(tmp_path, fetch=fetch, environ=env, machine="x86_64")
    assert urls[0].startswith(f"https://github.com/aquasecurity/trivy/releases/download/v{hostscan.TRIVY_VERSION}/") and urls[1] == urls[0]
    with pytest.raises(hostscan.ScanError):
        hostscan.download(tmp_path, fetch=fetch, environ={"KAJI_TRIVY_URL": "https://artifacts.corp/trivy/"}, machine="x86_64")
    assert urls[2] == f"https://artifacts.corp/trivy/{NAME}"


def test_a_blank_trivy_bin_is_no_setting_and_the_database_repository_reaches_trivy(tmp_path):
    assert hostscan.find_trivy(tmp_path, environ={"KAJI_TRIVY_BIN": " "}, which=lambda n: "/usr/bin/trivy") == Path("/usr/bin/trivy")
    seen = []

    def run(cmd, **kw):
        seen.append(kw["env"])
        Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps(TRIVY))
        return SimpleNamespace(returncode=0, stderr="")

    scan(tmp_path, run=run, env={"KAJI_TRIVY_DB_REPOSITORY": "registry.corp/db:2"})
    scan(tmp_path, run=run)
    assert seen[0]["TRIVY_DB_REPOSITORY"] == "registry.corp/db:2" and "TRIVY_DB_REPOSITORY" not in seen[1]
