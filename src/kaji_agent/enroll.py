"""Enrolling a host: an organization's **enrollment token** (``ke_...``, made in the console) lets a new host agent register itself instead of being made one by
one. The agent sends the token with its hostname, the gateway makes the host agent in that organization and answers with the agent's own token (``kb_...``),
and the agent keeps it in ``<state dir>/token``: from then on that file is its token and the enrollment token is not used again.

The enrollment token can make host agents and nothing else; it never reports or builds. The agent's own token is written with mode 0600."""

import asyncio
import json
import os
import platform
from pathlib import Path

import websockets

STATE_DIR = "/var/lib/kaji-agent"  # the systemd unit's StateDirectory
TOKEN_FILE = "token"
TIMEOUT = 30  # seconds to wait for the gateway's answer


def state_dir():
    return Path(os.environ.get("KAJI_STATE_DIR") or STATE_DIR)


def saved_token(directory):
    """The agent's own token kept by an earlier enrollment, or ``""``."""
    try:
        token = (Path(directory) / TOKEN_FILE).read_text().strip()
    except OSError:
        return ""
    return token if token.startswith("kb_") else ""


def save_token(directory, token):
    """Keep the token, readable by the agent only; written whole, then moved into place, so a crash leaves no half token."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{TOKEN_FILE}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.replace(tmp, directory / TOKEN_FILE)


async def enroll(url, enroll_token, hostname=None, name="", connect=websockets.connect):
    """Register this host: ``(agent name, agent token)``. Exits with the gateway's reason if it refuses (a token that is revoked, expired or used up, the plan's
    limit of hosts, a host of that name that is already there)."""
    try:
        async with connect(url, additional_headers={"Authorization": f"Bearer {enroll_token}"}) as ws:
            await ws.send(json.dumps({"type": "enroll", "hostname": hostname or platform.node(), **({"name": name} if name else {})}))
            answer = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT))
    except websockets.exceptions.InvalidStatus as err:
        raise SystemExit("the gateway refused the enrollment token: is it the one shown in the console, and not mistyped?") from err
    except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as err:
        raise SystemExit(f"could not enroll with {url}: {err}") from err
    if answer.get("type") == "refused":
        raise SystemExit(f"the gateway refused to enroll this host: {answer.get('reason') or 'no reason given'}")
    if answer.get("type") != "enrolled" or not str(answer.get("token", "")).startswith("kb_"):
        raise SystemExit(f"the gateway did not enroll this host: it answered {answer.get('type')!r}")
    return str(answer.get("name", "")), answer["token"]
