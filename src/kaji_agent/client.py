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

log = logging.getLogger("kaji_agent")
BACKOFF_MAX = 60


def node_info():
    return {"hostname": platform.node(), "arch": platform.machine(), "system": platform.system()}


class Agent:
    def __init__(self, url, token, handler=None):
        self.url, self.token = url, token
        self.handler = handler  # async callable(task_uid, payload): runs a task; None until builds are supported

    async def session(self, ws):
        await ws.send(json.dumps({"type": "hello", "info": node_info()}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "task":
                await ws.send(json.dumps({"type": "ack", "task": msg["task"]}))
                if self.handler:
                    await self.handler(msg["task"], msg["payload"])
            elif msg.get("type") == "welcome":
                log.info("connected to %s", self.url)

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
