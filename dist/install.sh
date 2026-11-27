#!/bin/sh
# Installs the kaji host agent on this machine and enrolls it, with the enrollment token of a host made in the console (Hosts, Add host). Two ways:
#   binary  (the default where there is systemd or OpenRC): downloads the agent from the release, checks its checksum, installs it as a service;
#   docker  (the default where there is only docker): runs the agent's image as a container that restarts with the machine, with what it reads mounted read-only.
# Run it as root with the token:
#
#   curl -fsSL https://github.com/Kaji-Secure/build-agent/releases/latest/download/install.sh | sudo sh -s -- ke_...
#
# Options: --binary or --docker (force the way; --mode binary|docker|auto), --name NAME (the name to enroll under: a host made in the console has its own),
# --url WSS_URL (another gateway), --version X.Y.Z (default: the latest).
# The token may also be in KAJI_ENROLL_TOKEN. It is used once: the agent then keeps its own token in /var/lib/kaji-agent/token and the spent one is taken out of
# the settings. For x86_64 and aarch64. Installed: /usr/bin/kaji-agent, the service, /etc/kaji-agent/kaji-agent.env or /etc/conf.d/kaji-agent (mode 0600), and,
# with OpenRC, a system user kaji-agent. Run again with a new token to enroll again; it replaces the binary and the service files and nothing else.
set -eu

REPO=Kaji-Secure/build-agent
token="${KAJI_ENROLL_TOKEN:-}"; name="${KAJI_NAME:-}"; url="${KAJI_URL:-}"; version=""; mode="${KAJI_INSTALL_MODE:-auto}"
while [ $# -gt 0 ]; do
	case "$1" in
		--name) name="$2"; shift 2 ;;
		--url) url="$2"; shift 2 ;;
		--version) version="$2"; shift 2 ;;
		--mode) mode="$2"; shift 2 ;;
		--binary) mode=binary; shift ;;
		--docker) mode=docker; shift ;;
		ke_*) token="$1"; shift ;;
		-h|--help) sed -n '2,14p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "unknown argument: $1 (the token starts with ke_)" >&2; exit 2 ;;
	esac
done

die() { echo "kaji-agent install: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run this as root: ... | sudo sh -s -- ke_..."
case "$(uname -m)" in x86_64) arch=amd64 ;; aarch64|arm64) arch=arm64 ;; *) die "this release has the agent for x86_64 and aarch64 only" ;; esac
case "$token" in ke_*) ;; *) grep -qs '^kb_' /var/lib/kaji-agent/token || die "no enrollment token: pass it as the argument (ke_...) or in KAJI_ENROLL_TOKEN" ;; esac
case "$mode" in auto|binary|docker) ;; *) die "--mode is binary, docker or auto" ;; esac

# docker: the agent's image as a container. It runs as the image's user (nobody) with a read-only root, no capabilities and only what it reads mounted
# (read-only), keeps its token in /var/lib/kaji-agent, and restarts with the machine. After it has enrolled it is made again without the spent token.
run_container() {
	docker rm -f kaji-agent >/dev/null 2>&1 || true
	mounts="-v /var/lib/kaji-agent:/state -v /sys:/sys:ro"
	# /var/db/repos too: make.profile points into it, and the profile (with its parents) is read from there
	for d in /var/db/pkg /etc/portage /var/db/repos; do [ -d "$d" ] && mounts="$mounts -v $d:$d:ro"; done
	# shellcheck disable=SC2086
	docker run -d --name kaji-agent --restart unless-stopped --hostname "$(hostname)" --env-file "$settings" $mounts \
		--read-only --tmpfs /tmp:rw,exec,mode=1777 --cap-drop ALL --security-opt no-new-privileges "$image" --role host >/dev/null
}

install_docker() {
	command -v docker >/dev/null || die "docker is not installed: install it, or use --binary for a service without it"
	[ -d /var/db/pkg ] || echo "note: no /var/db/pkg here: the agent reads a Gentoo host's installed packages, so it will report little" >&2
	image="ghcr.io/kaji-secure/kaji-agent:${version:+${version#v}}"; image="${image%:}"; [ "$image" = "ghcr.io/kaji-secure/kaji-agent" ] && image="$image:latest"
	echo "pulling $image ..."
	docker pull -q "$image" >/dev/null || die "could not pull $image"
	umask 077
	install -d -m 0700 /etc/kaji-agent
	install -d -o 65534 -g 65534 -m 0700 /var/lib/kaji-agent
	settings=/etc/kaji-agent/docker.env
	{
		echo "KAJI_ROLE=host"
		echo "KAJI_STATE_DIR=/state"
		[ -n "$token" ] && echo "KAJI_ENROLL_TOKEN=$token"
		[ -n "$name" ] && echo "KAJI_NAME=$name"
		[ -n "$url" ] && echo "KAJI_URL=$url"
		true
	} > "$settings"
	chmod 0600 "$settings"
	run_container
	echo "waiting for the agent to enroll ..."
	for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
		grep -qs '^kb_' /var/lib/kaji-agent/token && break
		sleep 5
	done
	if grep -qs '^kb_' /var/lib/kaji-agent/token; then
		sed -i '/^KAJI_ENROLL_TOKEN=/d' "$settings"
		run_container  # again, without the spent token in its settings: it starts with the token it kept
		echo "done: this host is enrolled and the agent runs as the container kaji-agent; it appears in the console under Hosts."
	else
		echo "the container is running but the agent did not enroll yet. Its log:" >&2
		docker logs --tail 8 kaji-agent 2>&1 | sed -E 's/(ke|kb)_[A-Za-z0-9_-]+/\1_***/g' >&2
		echo "Common causes: the token is revoked, expired or used up (make a new command in the console), or the plan's limit of hosts is reached." >&2
		exit 1
	fi
}

if [ "$mode" = auto ]; then
	if [ -d /run/systemd/system ] || command -v rc-service >/dev/null; then mode=binary
	elif command -v docker >/dev/null; then mode=docker
	else die "found neither systemd, OpenRC nor docker: run /usr/bin/kaji-agent --role host by hand (see docs/hosts.rst)"; fi
fi
if [ "$mode" = docker ]; then install_docker; exit 0; fi

command -v curl >/dev/null || die "curl is needed"
command -v sha256sum >/dev/null || die "sha256sum is needed"

if [ -n "$version" ]; then base="https://github.com/$REPO/releases/download/v${version#v}"; else base="https://github.com/$REPO/releases/latest/download"; fi
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
echo "downloading the kaji agent ${version:-(latest)} ..."
curl -fsSL -o "$work/kaji-agent-linux-$arch" "$base/kaji-agent-linux-$arch" || die "could not download $base/kaji-agent-linux-$arch"
curl -fsSL -o "$work/kaji-agent-linux-$arch.sha256" "$base/kaji-agent-linux-$arch.sha256" || die "could not download the checksum"
(cd "$work" && sha256sum -c kaji-agent-linux-$arch.sha256) || die "the checksum does not match: nothing was installed"

if [ -d /run/systemd/system ]; then init=systemd; elif command -v rc-service >/dev/null; then init=openrc; else die "neither systemd nor OpenRC found: run /usr/bin/kaji-agent --role host by hand (see docs/hosts.rst)"; fi
[ "$init" = systemd ] && { systemctl stop kaji-agent 2>/dev/null || true; } || { rc-service kaji-agent stop >/dev/null 2>&1 || true; }
install -m 0755 "$work/kaji-agent-linux-$arch" /usr/bin/kaji-agent

umask 077
if [ "$init" = systemd ]; then
	cat > /etc/systemd/system/kaji-agent.service <<'EOF_UNIT'
[Unit]
Description=kaji host agent: reports this machine, its packages and its /etc/portage to kaji
Documentation=https://github.com/Kaji-Secure/build-agent
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/kaji-agent --role host
EnvironmentFile=/etc/kaji-agent/kaji-agent.env
# The compiled agent unpacks itself on start; /tmp may be noexec, so it uses its own directory.
Environment=TMPDIR=/var/lib/kaji-agent
Restart=always
RestartSec=10

# It reads /var/db/pkg, /etc/portage and /sys, and talks out to kaji: nothing else.
DynamicUser=yes
StateDirectory=kaji-agent
ProtectSystem=strict
ProtectHome=yes
PrivateDevices=yes
PrivateTmp=yes
NoNewPrivileges=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=yes
LockPersonality=yes
CapabilityBoundingSet=
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
EOF_UNIT
	chmod 0644 /etc/systemd/system/kaji-agent.service
	install -d -m 0700 /etc/kaji-agent
	settings=/etc/kaji-agent/kaji-agent.env
else
	getent group kaji-agent >/dev/null || groupadd -r kaji-agent
	getent passwd kaji-agent >/dev/null || useradd -r -g kaji-agent -d /var/lib/kaji-agent -s /sbin/nologin kaji-agent
	install -d -o kaji-agent -g kaji-agent -m 0750 /var/lib/kaji-agent
	cat > /etc/init.d/kaji-agent <<'EOF_INITD'
#!/sbin/openrc-run
# The kaji host agent: reports this machine, its packages and its /etc/portage to kaji (docs/hosts.rst in the kaji repository).

description="kaji host agent"
command="/usr/bin/kaji-agent"
command_args="--role host"
command_user="kaji-agent:kaji-agent"
supervisor="supervise-daemon"
output_log="/var/log/kaji-agent.log"
error_log="/var/log/kaji-agent.log"

depend() {
	need net
	after dns
}

start_pre() {
	if [ -z "${KAJI_TOKEN}" ] && [ -z "${KAJI_ENROLL_TOKEN}" ] && ! grep -qs '^kb_' /var/lib/kaji-agent/token; then
		eerror "set KAJI_TOKEN (the token of an agent of role host) or KAJI_ENROLL_TOKEN (an enrollment token) in /etc/conf.d/kaji-agent"
		return 1
	fi
	checkpath --directory --owner kaji-agent:kaji-agent --mode 0750 /var/lib/kaji-agent
	checkpath --file --owner kaji-agent:kaji-agent --mode 0640 /var/log/kaji-agent.log
	# the compiled agent unpacks itself on start: into its own directory, as /tmp may be noexec
	export TMPDIR=/var/lib/kaji-agent
	export KAJI_TOKEN KAJI_ENROLL_TOKEN KAJI_NAME KAJI_URL KAJI_REPORT_INTERVAL KAJI_HOST_SCAN KAJI_HOST_ROOT KAJI_TRIVY_URL KAJI_TRIVY_BIN KAJI_TRIVY_DB_REPOSITORY TRIVY_USERNAME TRIVY_PASSWORD
}
EOF_INITD
	chmod 0755 /etc/init.d/kaji-agent
	settings=/etc/conf.d/kaji-agent
fi

# the settings: systemd reads KEY=value, OpenRC's file is shell (KEY="value"); the token is a name made of url-safe characters
q='"'; [ "$init" = systemd ] && q=''
{
	echo "# The kaji host agent (see docs/hosts.rst). Mode 0600, root only: it holds a token."
	echo "KAJI_TOKEN=${q}${q}"
	[ -n "$token" ] && echo "KAJI_ENROLL_TOKEN=${q}${token}${q}"
	[ -n "$name" ] && echo "KAJI_NAME=${q}${name}${q}"
	[ -n "$url" ] && echo "KAJI_URL=${q}${url}${q}"
	true
} > "$settings"
chmod 0600 "$settings"

if [ "$init" = systemd ]; then
	systemctl daemon-reload
	systemctl enable --now kaji-agent
else
	rc-update add kaji-agent default >/dev/null
	rc-service kaji-agent start
fi

echo "waiting for the agent to enroll ..."
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
	grep -qs '^kb_' /var/lib/kaji-agent/token && break
	sleep 5
done
if grep -qs '^kb_' /var/lib/kaji-agent/token; then
	sed -i '/^KAJI_ENROLL_TOKEN=/d' "$settings"  # spent: the agent has its own token now
	echo "done: this host is enrolled and the agent is running; it appears in the console under Hosts."
else
	echo "the agent is installed but did not enroll yet. Its log:" >&2
	if [ "$init" = systemd ]; then journalctl -u kaji-agent -n 8 --no-pager 2>/dev/null | sed -E 's/(ke|kb)_[A-Za-z0-9_-]+/\1_***/g' >&2; else tail -n 8 /var/log/kaji-agent.log 2>/dev/null | sed -E 's/(ke|kb)_[A-Za-z0-9_-]+/\1_***/g' >&2; fi
	echo "Common causes: the token is revoked, expired or used up (make a new command in the console), or the plan's limit of hosts is reached." >&2
	exit 1
fi
