"""The agent's end of the connection: connect to the gateway, say hello, acknowledge the tasks it sends, and reconnect when the line drops.

The messages are those of ``waldmeister_web.console.builder_session``; this package does not import it, so the two are kept in step by the
protocol's tests on both sides.
"""

import asyncio
import json
import logging
import platform
import random

import websockets

from . import sysinfo
from .runner import Runner

log = logging.getLogger("kaji_agent")
BACKOFF_MAX = 60


UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "k": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "m": 0.001,
}


def quantity(text):
    """A Kubernetes quantity (``3920m``, ``16Gi``) as a number."""
    text = str(text)
    for unit in sorted(UNITS, key=len, reverse=True):
        if text.endswith(unit):
            return float(text[: -len(unit)]) * UNITS[unit]
    return float(text)


def cluster_capacity(kube):
    """What the cluster's nodes can run: their architectures and the size of the biggest one (a task is one pod on one node). Empty when the
    agent may not list nodes: the server then goes by the host's architecture."""
    try:
        nodes = (kube.request("GET", "/api/v1/nodes") or {}).get("items", [])
        return {
            "arches": sorted({n["status"]["nodeInfo"]["architecture"] for n in nodes}),
            "cpus": max(quantity(n["status"]["allocatable"]["cpu"]) for n in nodes),
            "memory": max(quantity(n["status"]["allocatable"]["memory"]) for n in nodes),
        }
    except (OSError, KeyError, ValueError):
        return {}


def node_info(kube=None):
    return {
        "hostname": platform.node(),
        "arch": platform.machine(),
        "system": platform.system(),
        **(cluster_capacity(kube) if kube else {}),
    }


class Agent:
    def __init__(self, url, token, kube=None):
        self.url, self.token = url, token
        self.kube = kube  # the cluster the tasks run in; without one the agent only acknowledges them

    async def session(self, ws):
        async def send(message):
            await ws.send(json.dumps(message))

        runner = Runner(self.kube, send) if self.kube else None
        running = set()  # the tasks being followed; a reference is kept so that they are not collected
        await send({"type": "hello", "info": await asyncio.to_thread(node_info, self.kube)})
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "task":
                    await send({"type": "ack", "task": msg["task"]})
                    if runner:
                        running.add(task := asyncio.create_task(runner.run(msg["task"], msg["payload"])))
                        task.add_done_callback(running.discard)
                elif msg.get("type") == "inspect":  # asked for, from the console: what the machine is, now
                    await send({"type": "hardware", "hardware": await asyncio.to_thread(sysinfo.collect, self.kube)})
                elif msg.get("type") == "welcome":
                    log.info("connected to %s", self.url)
                    if runner:
                        running.update(await runner.resume())
        finally:
            for task in running:
                task.cancel()  # the Jobs go on in the cluster; the next session picks them up again

    async def run(self):
        """Stay connected for ever. A refused token (HTTP 403) is not retried: it will not get better."""
        delay = 1
        while True:
            try:
                async with websockets.connect(
                    self.url, additional_headers={"Authorization": f"Bearer {self.token}"}
                ) as ws:
                    delay = 1
                    await self.session(ws)
            except websockets.exceptions.InvalidStatus as err:
                if err.response.status_code in (401, 403):
                    raise SystemExit(
                        "the gateway refused the token: make a new builder in the console and use its token"
                    ) from err
                log.warning("gateway answered %s", err.response.status_code)
            except (OSError, websockets.exceptions.WebSocketException) as err:
                log.warning("connection lost: %s", err)
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, BACKOFF_MAX)
