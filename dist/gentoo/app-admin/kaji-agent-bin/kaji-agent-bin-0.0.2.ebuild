# Copyright 2026 Kaji
# Distributed under the terms of the GNU General Public License v2

EAPI=8

inherit systemd

DESCRIPTION="kaji host agent: reports this machine, its packages and its /etc/portage to kaji"
HOMEPAGE="https://github.com/Kaji-Secure/build-agent"
# The binary is what the release workflow publishes (Nuitka; amd64 only for now).
SRC_URI="https://github.com/Kaji-Secure/build-agent/releases/download/v${PV}/kaji-agent-linux-amd64 -> ${P}-amd64"
S="${WORKDIR}"

LICENSE="all-rights-reserved"
SLOT="0"
KEYWORDS="-* ~amd64"
RESTRICT="bindist mirror strip"
QA_PREBUILT="usr/bin/kaji-agent"

RDEPEND="
	acct-group/kaji-agent
	acct-user/kaji-agent
"

src_unpack() {
	:
}

src_install() {
	newbin "${DISTDIR}/${P}-amd64" kaji-agent

	# the units and the settings of the same release (the files/ are copies of dist/ of the agent's repository)
	insinto /etc/kaji-agent
	doins "${FILESDIR}/kaji-agent.env"
	fperms 0600 /etc/kaji-agent/kaji-agent.env
	systemd_dounit "${FILESDIR}/kaji-agent.service"
	newinitd "${FILESDIR}/kaji-agent.initd" kaji-agent
	newconfd "${FILESDIR}/kaji-agent.confd" kaji-agent
	fperms 0600 /etc/conf.d/kaji-agent
}

pkg_postinst() {
	elog "Make an agent of role host in the console (Agents), and put its token into"
	elog "  /etc/kaji-agent/kaji-agent.env   (systemd)  or  /etc/conf.d/kaji-agent   (OpenRC)"
	elog "then:  systemctl enable --now kaji-agent   or   rc-update add kaji-agent default && rc-service kaji-agent start"
}
