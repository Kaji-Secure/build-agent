"""The version of this agent, as it says it in its hello (the console shows it, so that an old agent is seen).

It comes from git, through setuptools-scm, which writes ``_version.py`` when the agent is built: a tagged release is ``0.0.4``, a build of a later commit
``0.0.5.dev2+g1a2b3c4d`` (two commits after the tag, at commit 1a2b3c4d), so a build always says which code it is. The release builds pass the version
(``SETUPTOOLS_SCM_PRETEND_VERSION``, the Dockerfile's ``VERSION``) because their build context has no git. ``KAJI_AGENT_VERSION`` overrides it."""

import os
import re

try:
    from ._version import __version__ as STAMPED
except ImportError:  # not built (a checkout that was not installed): nothing wrote it
    STAMPED = ""


def agent_version():
    value = os.environ.get("KAJI_AGENT_VERSION") or STAMPED
    return value if value and not value.startswith("0.0.0+unknown") else "unknown"


def agent_commit():
    """The commit a development build is of (``g1a2b3c4d`` in its version), or ``""`` for a release."""
    match = re.search(r"\+g([0-9a-f]+)", agent_version())
    return match.group(1) if match else ""
