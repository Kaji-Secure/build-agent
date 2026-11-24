import json
import os

import pytest
from kaji_agent import hostinfo
from kaji_agent.client import Agent


def package(db, cpv, slot="0", repository="gentoo", use="ssl -X", rdepend=None):
    where = db / cpv
    where.mkdir(parents=True)
    if rdepend is not None:
        (where / "RDEPEND").write_text(rdepend + "\n")
    (where / "SLOT").write_text(slot + "\n")
    (where / "repository").write_text(repository + "\n")
    (where / "USE").write_text(use + "\n")


@pytest.fixture
def machine(tmp_path):
    db, etc = tmp_path / "db", tmp_path / "etc"
    package(db, "app-misc/figlet-2.2.5-r2", use="")
    package(db, "dev-libs/openssl-3.3.2", slot="0/3", use="asm ssl")
    package(db, "dev-lang/python-3.13.1", slot="3.13")
    package(db, "app-misc/needy-1", rdepend=">=dev-libs/openssl-3:0/3= dev-lang/python:3.13[ssl] !<app-misc/old-2 app-misc/gone")
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
        "app-misc/needy-1",
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
        "dependencies" not in lock["packages"]["dev-libs/openssl-3.3.2"]  # pap leaves it out when there are none
        and lock["packages"]["dev-libs/openssl-3.3.2"]["slot"] == "0"
    )
    assert lock["packages"]["app-misc/needy-1"]["dependencies"] == {
        "app-misc/gone": {},
        "dev-lang/python-3.13.1": {"repository": "gentoo"},
        "dev-libs/openssl-3.3.2": {"repository": "gentoo"},
    }
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


def test_a_profile_this_process_cannot_read_is_still_named(profiles, tmp_path):
    etc, repos, _ = profiles
    # a container without /var/db/repos: the link is there, what it points to is not
    os.symlink("/var/db/repos/oz/profiles/red-5", os.path.join(etc, "make.profile"))
    assert hostinfo.profile(etc, repos) == {"name": "oz:red-5", "chain": [{"name": "oz:red-5", "files": {}}]}


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


INSTALLED = {
    "dev-lang/python": [("dev-lang/python-3.12.1", {"slot": "3.12", "repository": "gentoo"}), ("dev-lang/python-3.13.1", {"slot": "3.13"})],
    "sys-libs/glibc": [("sys-libs/glibc-2.43-r4", {"slot": "2.2", "repository": "gentoo"})],
    "sys-devel/gcc": [("sys-devel/gcc-16.2.1_p20260926", {"slot": "16", "repository": "gentoo"})],
    "llvm-runtimes/clang-runtime": [("llvm-runtimes/clang-runtime-22", {"slot": "22"})],
}


def needs(text, installed=INSTALLED):
    return hostinfo.dependencies(text, installed)


def test_dependencies_are_what_pap_writes_installed_ones_named_as_installed_and_the_rest_by_name():
    got = needs(">=dev-libs/jansson-2 sys-libs/glibc:2.2/2.43= virtual/udev x11-libs/libX11[abi_x86_64(-)]")
    assert got == {
        "dev-libs/jansson": {},
        "sys-libs/glibc-2.43-r4": {"repository": "gentoo"},
        "virtual/udev": {},
        "x11-libs/libX11": {},
    }
    assert list(got) == sorted(got)


def test_blockers_versions_slots_use_dependencies_and_repositories_are_not_part_of_a_name():
    got = needs("!<dev-libs/atk-2.46.0 !!sys-apps/x =sys-libs/glibc-2.43* ~dev-libs/a-1.2-r3 dev-libs/b:3/4=[x,-y] dev-libs/c::overlay >=dev-libs/d-2.0_rc1:1")
    assert sorted(got) == ["dev-libs/a", "dev-libs/b", "dev-libs/c", "dev-libs/d", "sys-libs/glibc-2.43-r4"]
    assert needs("") == {} and needs("   ") == {} and needs("not an atom, 12 ((") == {}


def test_the_slot_of_an_atom_picks_the_installed_package_of_that_slot_and_one_package_is_listed_once():
    assert list(needs("dev-lang/python:3.13")) == ["dev-lang/python-3.13.1"]
    assert list(needs("dev-lang/python:3.12 dev-lang/python:3.13 dev-lang/python:3.13")) == ["dev-lang/python-3.12.1", "dev-lang/python-3.13.1"]  # one entry for each slot named, once
    assert list(needs("dev-lang/python:3.99")) == ["dev-lang/python"]  # no such slot installed: by name
    assert list(needs("dev-lang/python")) == ["dev-lang/python-3.12.1"]


def test_of_the_alternatives_of_a_choice_the_first_installed_counts_else_the_first_and_flags_already_reduced_are_skipped():
    assert list(needs("|| ( >=sys-devel/gcc-4.2:*[openmp] llvm-runtimes/clang-runtime:*[openmp] ) >=sys-libs/glibc-2.41-r6")) == [
        "sys-devel/gcc-16.2.1_p20260926", "sys-libs/glibc-2.43-r4"]
    assert list(needs("|| ( app-misc/none llvm-runtimes/clang-runtime )")) == ["llvm-runtimes/clang-runtime-22"]
    assert list(needs("|| ( app-misc/none app-misc/nope )")) == ["app-misc/none"]
    assert list(needs("|| ( ( app-misc/a app-misc/b ) sys-devel/gcc )")) == ["sys-devel/gcc-16.2.1_p20260926"]
    assert list(needs("ssl? ( dev-libs/openssl ) || ( ")) == ["dev-libs/openssl"]  # a cut-off string does not raise
