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
