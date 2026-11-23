# Copyright 2026 Kaji
# Distributed under the terms of the GNU General Public License v2

EAPI=8

inherit acct-user

DESCRIPTION="User for the kaji host agent"
ACCT_USER_ID=-1
ACCT_USER_GROUPS=( kaji-agent )
ACCT_USER_HOME=/var/lib/kaji-agent
ACCT_USER_HOME_PERMS=0750

acct-user_add_deps
