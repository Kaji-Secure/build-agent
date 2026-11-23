import argparse
import asyncio
import logging
import os

from .client import BUILT, ROLES, Agent
from .kube import Kube


def main():
    parser = argparse.ArgumentParser(prog="kaji-agent", description="Run kaji builds on this node.")
    parser.add_argument("--url", default=os.environ.get("KAJI_URL", "wss://console.kaji.systems/agent/ws"))
    parser.add_argument("--token", default=os.environ.get("KAJI_TOKEN", ""), help="the builder's token (or KAJI_TOKEN)")
    parser.add_argument(
        "--role",
        choices=ROLES,
        default=os.environ.get("KAJI_ROLE", "builder"),
        help="what this agent does (or KAJI_ROLE): it must be what its token was made for",
    )
    args = parser.parse_args()
    if args.role not in BUILT:
        parser.error(f"the {args.role} role is not built yet: this version runs {', '.join(BUILT)}")
    if not args.token:
        parser.error("a token is needed: --token or KAJI_TOKEN")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    kube = Kube.in_cluster() if os.environ.get("KUBERNETES_SERVICE_HOST") else None
    if not kube:
        logging.warning("not running in a Kubernetes cluster: tasks are acknowledged, not run")
    asyncio.run(Agent(args.url, args.token, kube, args.role).run())


if __name__ == "__main__":
    main()
