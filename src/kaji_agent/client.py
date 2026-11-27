"""The agent's end of the connection: connect to the gateway, say hello, acknowledge the tasks it sends, and reconnect when the line drops.

The messages are those of ``waldmeister_web.console.builder_session``; this package does not import it, so the two are kept in step by the
protocol's tests on both sides.
"""

import asyncio
import json
import logging
import os
import platform
import random
import time

import websockets

from . import clusterinfo, hostinfo, hostscan, imagescan, nodeprobe, sysinfo
from .kube import KubeError
from .sysinfo import schedulable  # noqa: F401  (what a node can take: see there)
from .runner import Runner
from .version import agent_version

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
    """What the cluster's nodes can run: their architectures, the size of the biggest one (``cpus``, ``memory``: a task is one pod on one node) and
    the CPUs of all of them together (``cluster_cpus``: what the agent could use if the user did not limit it). The agent is one per cluster and its
    Jobs go to whichever node the scheduler picks, so the nodes that can take a pod count, and a tainted control plane does not. Empty when the agent
    may not list nodes: the server then goes by the host's architecture."""
    try:
        nodes = [n for n in (kube.request("GET", "/api/v1/nodes") or {}).get("items", []) if schedulable(n)]
        return {
            "arches": sorted({n["status"]["nodeInfo"]["architecture"] for n in nodes}),
            "cpus": max(quantity(n["status"]["allocatable"]["cpu"]) for n in nodes),
            "cluster_cpus": sum(quantity(n["status"]["allocatable"]["cpu"]) for n in nodes),
            "memory": max(quantity(n["status"]["allocatable"]["memory"]) for n in nodes),
        }
    except (OSError, KeyError, ValueError):
        return {}


ROLES = ("builder", "host", "cluster")  # what an agent does; the gateway holds it to the role its token was made for
BUILT = (
    "builder",
    "host",
)  # the roles this version can run (cluster: ROADMAP 11 in the kaji repository)
REPORT_EVERY = 6 * 3600  # seconds between a host's reports; KAJI_REPORT_INTERVAL


def node_info(kube=None, role="builder"):
    """What the agent says in its hello. ``role`` is what it does. ``cpus`` is the biggest node's logical CPUs (to know whether a task fits); ``threads_per_core`` turns CPUs
    into cores; ``cpu_budget`` is the cores the user gave kaji, if they said (``KAJI_CPUS``, or the container's CPU limit): the gateway counts it
    against the plan and never gives this agent more work than it. ``version`` is this agent's release, so that the console can show which are old."""
    budget, source = sysinfo.cpu_budget()
    return {
        "role": role,
        "version": agent_version(),
        "hostname": platform.node(),
        "arch": platform.machine(),
        "system": platform.system(),
        "threads_per_core": sysinfo.threads_per_core(),
        **({"cpu_budget": budget, "cpu_budget_from": source} if budget else {}),
        **(cluster_capacity(kube) if kube else {}),
    }


class Agent:
    def __init__(self, url, token, kube=None, role="builder", interval=REPORT_EVERY):
        self.url, self.token, self.role, self.interval = url, token, role, interval
        self.kube = kube  # the cluster the tasks run in; without one the agent only acknowledges them
        self.cluster_reports = os.environ.get("KAJI_CLUSTER_REPORT", "1") != "0"  # the chart's clusterReport: false switches the cluster report off
        self.cluster_warned = False
        self.image_scans = os.environ.get("KAJI_IMAGE_SCAN", "1") != "0"  # the chart's imageScan: false never starts a Trivy Job
        self.trivy_image = imagescan.trivy_image()  # an internal registry's, or the public one when KAJI_TRIVY_IMAGE is not set
        self.trivy_db = imagescan.setting("KAJI_TRIVY_DB_REPOSITORY")  # an internal mirror of Trivy's database; empty: the public one
        self.scanned = {}  # digest -> (when, whether it worked): what was scanned lately, so that the timer does not scan an image again at once
        self.scanning = False
        self.host_scans = os.environ.get("KAJI_HOST_SCAN", "1") != "0"  # a host agent scans its own packages with Trivy (hostscan.py); 0 never does
        self.host_scanned = None  # (when, whether it worked) of the last scan of this host

    async def session(self, ws):
        async def send(message):
            await ws.send(json.dumps(message))

        runner = (
            Runner(self.kube, send) if self.kube and self.role == "builder" else None
        )

        async def scan_host():  # then its vulnerabilities, if it is due: Trivy, fetched if the host has none (hostscan.py); it takes minutes, so not in the report's way
            try:
                result = await asyncio.to_thread(hostscan.scan)
                self.host_scanned = (time.monotonic(), not result["error"])
                if result["error"]:
                    log.warning("could not scan this host: %s", result["error"])
                await send({"type": "report", "kind": "hostscan", "report": result})
            finally:
                self.scanning = False

        async def report():  # a host says what it is and what is installed: read off the event loop, it walks the package database
            await send(
                {
                    "type": "report",
                    "kind": "host",
                    "report": await asyncio.to_thread(hostinfo.report),
                }
            )
            if self.host_scans and not self.scanning and hostscan.due(self.host_scanned, time.monotonic()):
                self.scanning = True
                running.add(task := asyncio.create_task(scan_host()))
                task.add_done_callback(running.discard)

        async def cluster_report():  # a builder in a cluster also says what the cluster is: its nodes' OS and the images that run in it (clusterinfo.py)
            if not (self.kube and self.role == "builder" and self.cluster_reports):
                return
            try:
                found = await asyncio.to_thread(clusterinfo.report, self.kube)
            except (KubeError, OSError) as err:  # a report that fails is tried again at the next time, and the builder goes on building
                log.warning("could not read the cluster: %s", err)
                return
            if found:
                await send({"type": "report", "kind": "cluster", "report": found})
                if self.image_scans and not self.scanning:
                    self.scanning = True
                    running.add(task := asyncio.create_task(scan_images(found.get("images") or [])))  # a scan takes minutes: the timer goes on meanwhile
                    task.add_done_callback(running.discard)
            elif not self.cluster_warned:
                self.cluster_warned = True
                log.info("the cluster is not reported: the agent may not list nodes and pods (clusterReport in the chart)")

        async def scan_images(images):  # the images that run and were not scanned lately: a Trivy Job each (imagescan.py), the findings sent as each one ends
            try:
                wanted = imagescan.pending(images, self.scanned)
                if not wanted:
                    return
                secrets = await asyncio.to_thread(imagescan.pull_secrets_of_agent, self.kube)
                slots = asyncio.Semaphore(imagescan.PARALLEL)

                async def one(image, digest):
                    async with slots:
                        result = await asyncio.to_thread(
                            imagescan.scan, self.kube, image, digest, self.trivy_image, secrets, self.trivy_db
                        )
                    self.scanned[digest] = (time.monotonic(), not result["error"])
                    if result["error"]:
                        log.warning("could not scan %s: %s", image, result["error"])
                    await send({"type": "report", "kind": "images", "report": {"images": [result]}})

                await asyncio.gather(*(one(i, d) for i, d in wanted))
            except (KubeError, OSError) as err:  # the next report tries again
                log.warning("could not scan the images: %s", err)
            finally:
                self.scanning = False

        async def every():
            while True:
                await (report() if self.role == "host" else cluster_report())
                await asyncio.sleep(self.interval)

        reporting = None
        running = set()  # the tasks being followed; a reference is kept so that they are not collected
        probing = []  # the probe of the nodes' CPUs that is running, if one is: the console asking again meanwhile does not start another

        async def report_cpus(names):
            models = await asyncio.to_thread(nodeprobe.cpu_models, self.kube, names)
            if models:
                await send({"type": "hardware", "hardware": await asyncio.to_thread(sysinfo.collect, self.kube, cpu_models=models)})

        await send({"type": "hello", "info": await asyncio.to_thread(node_info, self.kube, self.role)})
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "task":
                    await send({"type": "ack", "task": msg["task"]})
                    if runner:
                        running.add(task := asyncio.create_task(runner.run(msg["task"], msg["payload"])))
                        task.add_done_callback(running.discard)
                elif (
                    msg.get("type") == "inspect" and self.role == "host"
                ):  # asked for, from the console: everything, now
                    await report()
                elif msg.get("type") == "cancel":  # the task is dropped: take its Job away (a Job that is gone is fine)
                    if runner:
                        try:
                            await runner.cancel(msg["task"])
                        except KubeError as err:  # say so (it was silent): usually the Role has no "delete" on jobs, as the charts before 0.0.4 did not
                            log.warning("could not remove the Job of task %s: %s", msg["task"], err)
                elif msg.get("type") == "inspect":  # asked for, from the console: what the machine is, now
                    hardware = await asyncio.to_thread(sysinfo.collect, self.kube)
                    await send({"type": "hardware", "hardware": hardware})
                    running.add(task := asyncio.create_task(cluster_report()))  # listing every pod takes a while: not before the answers below
                    task.add_done_callback(running.discard)
                    names = [n["name"] for n in hardware.get("nodes") or [] if n.get("name")]
                    if self.kube and names and not probing:  # then, as the answers come, the CPU of each node (a Job on it, which takes a while)
                        probing.append(task := asyncio.create_task(report_cpus(names)))
                        running.add(task)
                        task.add_done_callback(lambda t: (probing.remove(t), running.discard(t)))
                elif (
                    msg.get("type") == "refused"
                ):  # not allowed on this plan: fail, and say why, instead of retrying for ever
                    raise SystemExit(f"the gateway refused this agent: {msg.get('reason') or 'no reason given'}")
                elif msg.get("type") == "welcome":
                    log.info("connected to %s", self.url)
                    if runner:
                        running.update(await runner.resume())
                    if self.role in ("host", "builder") and reporting is None:
                        reporting = asyncio.create_task(
                            every()
                        )  # at once, then on the timer
        finally:
            if reporting:
                reporting.cancel()
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
