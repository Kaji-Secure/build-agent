"""The files a host is installed with (dist/): the units, the settings and the ebuild's copies of them, kept in step."""

import pathlib

import pytest

DIST = pathlib.Path(__file__).resolve().parents[1] / "dist"
FILES = DIST / "gentoo/app-admin/kaji-agent-bin/files"
COPIES = {
    "systemd/kaji-agent.service": "kaji-agent.service",
    "openrc/kaji-agent.initd": "kaji-agent.initd",
    "openrc/kaji-agent.confd": "kaji-agent.confd",
    "kaji-agent.env": "kaji-agent.env",
}


@pytest.mark.parametrize("source,copy", COPIES.items())
def test_the_ebuilds_copy_is_the_file_of_dist(source, copy):
    assert (DIST / source).read_text() == (FILES / copy).read_text(), f"cp dist/{source} to the ebuild's files/{copy}"


def test_both_services_run_the_host_role_and_name_where_the_token_comes_from():
    unit, init = (DIST / "systemd/kaji-agent.service").read_text(), (DIST / "openrc/kaji-agent.initd").read_text()
    assert (
        "ExecStart=/usr/bin/kaji-agent --role host" in unit and "EnvironmentFile=/etc/kaji-agent/kaji-agent.env" in unit
    )
    assert 'command_args="--role host"' in init and "KAJI_TOKEN" in init and "KAJI_ENROLL_TOKEN" in init


def test_the_unit_gives_the_agent_no_more_than_it_reads():
    unit = (DIST / "systemd/kaji-agent.service").read_text()
    for line in (
        "DynamicUser=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "PrivateDevices=yes",
    ):
        assert line in unit.splitlines()


def test_the_token_files_are_not_world_readable_in_the_ebuild():
    ebuild = (DIST / "gentoo/app-admin/kaji-agent-bin/kaji-agent-bin-0.0.2.ebuild").read_text()
    assert "fperms 0600 /etc/kaji-agent/kaji-agent.env" in ebuild and "fperms 0600 /etc/conf.d/kaji-agent" in ebuild


def _embedded(script, marker):
    """What install.sh writes with ``cat > file <<'marker'``."""
    start = script.index(f"<<'{marker}'\n") + len(f"<<'{marker}'\n")
    return script[start : script.index(f"\n{marker}\n", start) + 1]


def test_the_installer_embeds_the_units_of_dist_unchanged():
    script = (DIST / "install.sh").read_text()
    assert _embedded(script, "EOF_UNIT") == (DIST / "systemd/kaji-agent.service").read_text()
    assert _embedded(script, "EOF_INITD") == (DIST / "openrc/kaji-agent.initd").read_text()


def test_the_installer_is_valid_shell_that_checks_the_checksum_and_never_holds_a_token():
    import subprocess

    script = (DIST / "install.sh").read_text()
    assert subprocess.run(["sh", "-n", str(DIST / "install.sh")]).returncode == 0
    assert "sha256sum -c" in script and "set -eu" in script and "chmod 0600" in script
    import re

    assert not re.search(r"k[eb]_[A-Za-z0-9_-]{20,}", script)  # only placeholders: no real token


def test_the_installer_refuses_without_a_token_or_root_and_says_what_is_missing():
    import os
    import subprocess

    out = subprocess.run(["sh", str(DIST / "install.sh"), "--bogus"], capture_output=True, text=True)
    assert out.returncode == 2 and "unknown argument" in out.stderr
    if os.getuid() != 0:
        out = subprocess.run(["sh", str(DIST / "install.sh"), "ke_x"], capture_output=True, text=True)
        assert out.returncode == 1 and "run this as root" in out.stderr


def test_the_release_workflow_attaches_the_installer():
    workflow = (DIST.parent / ".github/workflows/binary.yml").read_text()
    assert "dist/install.sh" in workflow


def test_the_installer_has_both_ways_and_picks_by_what_the_machine_has():
    script = (DIST / "install.sh").read_text()
    for part in ("--binary", "--docker", "--mode", "install_docker", "run_container", "--read-only", "--cap-drop ALL", "--restart unless-stopped", "no-new-privileges"):
        assert part in script, part
    assert script.index("[ -d /run/systemd/system ] || command -v rc-service") < script.index("elif command -v docker")  # a service where there is an init, else docker
    assert "KAJI_ENROLL_TOKEN=$token" in script and "sed -i '/^KAJI_ENROLL_TOKEN=/d'" in script  # the spent token leaves the settings and the container is made again


def test_the_installer_rejects_a_mode_it_does_not_know():
    import os
    import subprocess

    if os.getuid() == 0:
        return
    out = subprocess.run(["sh", str(DIST / "install.sh"), "--mode", "foo", "ke_x"], capture_output=True, text=True)
    assert out.returncode == 1  # not root: stops first, before looking at the mode
