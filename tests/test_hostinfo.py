import json
import os

import pytest
from kaji_agent import hostinfo
from kaji_agent.client import Agent


def package(db, cpv, slot="0", repository="gentoo", use="ssl -X"):
    where = db / cpv
    where.mkdir(parents=True)
    (where / "SLOT").write_text(slot + "\n")
    (where / "repository").write_text(repository + "\n")
    (where / "USE").write_text(use + "\n")


@pytest.fixture
def machine(tmp_path):
    db, etc = tmp_path / "db", tmp_path / "etc"
    package(db, "app-misc/figlet-2.2.5-r2", use="")
    package(db, "dev-libs/openssl-3.3.2", slot="0/3", use="asm ssl")
    package(db, "dev-lang/python-3.13.1", slot="3.13")
    (db / "sys-libs").mkdir()
    (db / "sys-libs" / "-MERGING-glibc-2.40").mkdir()  # being merged: not installed yet
    (db / "stray-file").write_text("x")
    (etc / "package.use").mkdir(parents=True)
    (etc / "package.use" / "ssl").write_text("dev-libs/openssl asm\n")
    (etc / "make.conf").write_text(
        'USE="-X"\nPORTAGE_BINHOST="https://user:s3cret@bins.example.com/amd64"\n'
    )
    (etc / "gnupg").mkdir()
    (etc / "gnupg" / "secring.gpg").write_text("secret")
    (etc / "binhost.key").write_text("secret")
    (etc / "binary.bin").write_bytes(b"\xff\xfe\x00")
    (etc / "big").write_text("x" * (hostinfo.MAX_FILE + 1))
    (etc / "elsewhere").symlink_to("/etc/passwd")
    (etc / "make.profile").symlink_to(
        "/var/db/repos/gentoo/profiles/default/linux/amd64/23.0"
    )
    world = tmp_path / "world"
    world.write_text(
        "app-misc/figlet\n# a comment\ndev-lang/python:3.13\napp-misc/figlet\n"
    )
    return {"db": str(db), "etc": str(etc), "world_file": str(world)}


def test_installed_packages_are_read_from_the_package_database(machine):
    got = hostinfo.installed(machine["db"])
    assert sorted(got) == [
        "app-misc/figlet-2.2.5-r2",
        "dev-lang/python-3.13.1",
        "dev-libs/openssl-3.3.2",
    ]
    assert got["dev-libs/openssl-3.3.2"] == {
        "slot": "0",
        "repository": "gentoo",
        "use": ["asm", "ssl"],
    }  # the sub-slot is not the slot
    assert got["app-misc/figlet-2.2.5-r2"] == {"slot": "0", "repository": "gentoo"}
    assert hostinfo.installed("/nowhere") == {}


def test_the_world_file_is_what_the_user_chose_once_each(machine):
    assert hostinfo.world(machine["world_file"]) == [
        "app-misc/figlet",
        "dev-lang/python:3.13",
    ]
    assert hostinfo.world("/nowhere") == []


def test_the_portage_config_keeps_text_and_leaves_secrets_out(machine):
    config = hostinfo.portage_config(machine["etc"])
    assert sorted(config["files"]) == [
        "make.conf",
        "package.use/ssl",
    ]  # no key, no gnupg, no binary, no big file, no link
    assert (
        "s3cret" not in config["files"]["make.conf"]
        and "https://REDACTED@bins.example.com/amd64" in config["files"]["make.conf"]
    )
    assert config["profile"] == "default/linux/amd64/23.0"
    assert hostinfo.portage_config("/nowhere") == {"files": {}}


def test_the_lock_file_has_the_shape_pap_writes_and_changes_with_what_is_installed(
    machine,
):
    packages = hostinfo.installed(machine["db"])
    lock = hostinfo.lockfile(packages)
    assert (
        lock["lockfile_version"] == "1.0"
        and lock["generator"] == "kaji-agent"
        and lock["state_hash"].startswith("sha256:")
    )
    assert (
        lock["packages"]["dev-libs/openssl-3.3.2"]["dependencies"] == {}
        and lock["packages"]["dev-libs/openssl-3.3.2"]["slot"] == "0"
    )
    assert hostinfo.lockfile(packages)["state_hash"] == lock["state_hash"]
    assert hostinfo.lockfile({"a/b-1": {}})["state_hash"] != lock["state_hash"]


def test_the_package_builds_are_the_world_and_what_is_installed():
    builds = hostinfo.package_builds(
        ["app-misc/figlet"], {"b/c-2": {}, "a/b-1": {}}, "amd64"
    )
    assert builds["world"]["atoms"] == ["app-misc/figlet"] and builds["installed"][
        "atoms"
    ] == ["=a/b-1", "=b/c-2"]
    assert builds["world"]["arch"] == "amd64" and builds["world"]["planOnly"] is True
    assert hostinfo.package_builds([], {}, "") == {}


def test_disks_are_the_real_ones(tmp_path):
    for name, sectors, model, rotational in (
        ("sda", 2000, "Samsung SSD  870", "0"),
        ("nvme0n1", 4000, "", "0"),
        ("loop0", 10, "", "0"),
        ("sdb", 0, "", "1"),
    ):
        (tmp_path / name / "queue").mkdir(parents=True)
        (tmp_path / name / "device").mkdir()
        (tmp_path / name / "size").write_text(f"{sectors}\n")
        (tmp_path / name / "queue" / "rotational").write_text(rotational)
        if model:
            (tmp_path / name / "device" / "model").write_text(model + "\n")
    assert hostinfo.disks(str(tmp_path)) == [
        {"name": "nvme0n1", "size": 4000 * 512, "rotational": False},
        {
            "name": "sda",
            "size": 2000 * 512,
            "model": "Samsung SSD 870",
            "rotational": False,
        },
    ]
    assert hostinfo.disks("/nowhere") == []


def test_the_machine_is_what_the_firmware_says(tmp_path):
    (tmp_path / "sys_vendor").write_text("Dell Inc.\n")
    (tmp_path / "product_serial").write_text("SECRET\n")
    assert (
        hostinfo.machine(str(tmp_path)) == {"vendor": "Dell Inc."}
        and hostinfo.machine("/nowhere") == {}
    )


def test_the_report_has_the_machine_and_the_three_documents(machine):
    got = hostinfo.report(machine["etc"], machine["db"], machine["world_file"])
    assert {
        "hostname",
        "hardware",
        "portage_config",
        "lockfile",
        "package_builds",
    } <= set(got)
    json.dumps(got)  # it goes over the wire as JSON


class Socket:
    """What the agent sees of the gateway: the messages it was sent, and the ones it sends."""

    def __init__(self, incoming):
        self.sent, self.incoming = [], list(incoming)

    async def send(self, text):
        self.sent.append(json.loads(text))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return json.dumps(self.incoming.pop(0))


async def test_a_host_agent_reports_when_welcome_and_when_asked(monkeypatch):
    monkeypatch.setattr(hostinfo, "report", lambda: {"hostname": "h1"})
    ws = Socket([{"type": "welcome"}, {"type": "inspect"}])
    await Agent("ws://x", "kb_t", role="host", interval=3600).session(ws)
    kinds = [(m["type"], m.get("kind")) for m in ws.sent]
    assert kinds[0] == ("hello", None) and kinds.count(("report", "host")) >= 1
    assert all(
        m["type"] != "hardware" for m in ws.sent
    )  # a host answers the question with its report
    assert ws.sent[0]["info"]["role"] == "host"


async def test_a_builder_does_not_report(monkeypatch):
    monkeypatch.setattr(
        hostinfo, "report", lambda: pytest.fail("a builder reports no host")
    )
    ws = Socket([{"type": "welcome"}])
    await Agent("ws://x", "kb_t").session(ws)
    assert [m["type"] for m in ws.sent] == ["hello"]


@pytest.fixture
def profiles(tmp_path):
    """A repository with a profile that has a relative parent, a parent in a repository, and a loop back to itself."""
    repos = tmp_path / "repos"
    gentoo = repos / "gentoo" / "profiles"
    (gentoo / "default/linux/amd64/23.0/desktop").mkdir(parents=True)
    (gentoo / "default/linux/amd64/23.0/desktop/parent").write_text("..\n")
    (gentoo / "default/linux/amd64/23.0/desktop/package.use.mask").mkdir()
    (gentoo / "default/linux/amd64/23.0/desktop/package.use.mask/a").write_text(
        "dev-libs/x foo\n"
    )
    (gentoo / "default/linux/amd64/23.0/parent").write_text(
        "# the base\ngentoo:base\ngentoo:targets/loop\n"
    )
    (gentoo / "default/linux/amd64/23.0/make.defaults").write_text('USE="a b"\n')
    (gentoo / "base").mkdir()
    (gentoo / "base/make.defaults").write_text('FEATURES="x"\n')
    (gentoo / "targets/loop").mkdir(parents=True)
    (gentoo / "targets/loop/parent").write_text(
        "gentoo:default/linux/amd64/23.0/desktop\n"
    )
    (gentoo / "base/deep").mkdir()
    (gentoo / "base/deep/make.defaults").write_text(
        "not part of base: a profile of its own\n"
    )
    etc = tmp_path / "etc"
    etc.mkdir()
    return str(etc), str(repos), gentoo


def test_the_profile_is_read_with_its_parents_once_each_the_profile_first(profiles):
    etc, repos, gentoo = profiles
    os.symlink(
        gentoo / "default/linux/amd64/23.0/desktop", os.path.join(etc, "make.profile")
    )
    got = hostinfo.profile(etc, repos)
    assert got["name"] == "gentoo:default/linux/amd64/23.0/desktop"
    assert [c["name"] for c in got["chain"]] == [
        "gentoo:default/linux/amd64/23.0/desktop",
        "gentoo:default/linux/amd64/23.0",
        "gentoo:base",
        "gentoo:targets/loop",
    ]  # the loop ends where it began
    assert got["chain"][0]["files"] == {
        "parent": "..\n",
        "package.use.mask/a": "dev-libs/x foo\n",
    }
    assert got["chain"][1]["files"]["make.defaults"] == 'USE="a b"\n' and got["chain"][
        2
    ]["files"] == {"make.defaults": 'FEATURES="x"\n'}


def test_a_profile_that_is_not_there_is_empty(profiles, tmp_path):
    etc, repos, _ = profiles
    assert hostinfo.profile(etc, repos) == {}  # no make.profile
    os.symlink(tmp_path / "gone", os.path.join(etc, "make.profile"))
    assert hostinfo.profile(etc, repos) == {}


def test_a_missing_parent_is_left_out_and_a_big_file_is_not_read(profiles):
    etc, repos, gentoo = profiles
    (gentoo / "base/parent").write_text("gentoo:nowhere\n")
    (gentoo / "base/huge").write_text("x" * (hostinfo.MAX_FILE + 1))
    os.symlink(gentoo / "base", os.path.join(etc, "make.profile"))
    got = hostinfo.profile(etc, repos)
    assert [c["name"] for c in got["chain"]] == ["gentoo:base"] and sorted(
        got["chain"][0]["files"]
    ) == [
        "make.defaults",
        "parent",
    ]
