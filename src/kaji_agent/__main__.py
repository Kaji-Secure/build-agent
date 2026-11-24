import argparse
import asyncio
import logging
import os

from . import enroll, tls
from .client import BUILT, REPORT_EVERY, ROLES, Agent
from .kube import Kube


def main():
    parser = argparse.ArgumentParser(
        prog="kaji-agent",
        description="Run kaji builds on this node, or report this host to kaji.",
    )
    parser.add_argument("--url", default=os.environ.get("KAJI_URL", "wss://console.kaji.systems/agent/ws"))
    parser.add_argument("--token", default=os.environ.get("KAJI_TOKEN", ""), help="the builder's token (or KAJI_TOKEN)")
    parser.add_argument(
        "--enroll-token",
        default=os.environ.get("KAJI_ENROLL_TOKEN", ""),
        help="a host agent: an organization's enrollment token (or KAJI_ENROLL_TOKEN), to register this host and keep the token it gets in the state directory",
    )
    parser.add_argument("--name", default=os.environ.get("KAJI_NAME", ""), help="the name to enroll under (or KAJI_NAME; default: the hostname)")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("KAJI_REPORT_INTERVAL") or REPORT_EVERY),
        help="a host agent: seconds between its reports (or KAJI_REPORT_INTERVAL; default 6 hours)",
    )
    parser.add_argument(
        "--role",
        choices=ROLES,
        default=os.environ.get("KAJI_ROLE", "builder"),
        help="what this agent does (or KAJI_ROLE): it must be what its token was made for",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tls.use_system_certificates()  # before any connection: the enrollment is the first
    if args.role not in BUILT:
        parser.error(f"the {args.role} role is not built yet: this version runs {', '.join(BUILT)}")
    directory = enroll.state_dir()
    args.token = args.token or enroll.saved_token(directory)  # a host that enrolled before has its own token
    if not args.token and args.enroll_token:
        if args.role != "host":
            parser.error("an enrollment token is for host agents: use --role host")
        name, args.token = asyncio.run(enroll.enroll(args.url, args.enroll_token, name=args.name))
        try:
            enroll.save_token(directory, args.token)
        except OSError as err:  # the agent exists in the console but its token is lost: say so, it cannot be shown again
            raise SystemExit(
                f"enrolled as {name}, but its token could not be kept in {directory}: {err}. Revoke the agent {name} in the console, make the directory writable, and start again"
            ) from err
        logging.info("enrolled as %s; its token is kept in %s", name, directory / enroll.TOKEN_FILE)
    if not args.token:
        parser.error("a token is needed: --token or KAJI_TOKEN (a host agent may use --enroll-token or KAJI_ENROLL_TOKEN instead)")
    if args.interval < 60:
        parser.error("the report interval is at least 60 seconds")
    kube = (
        Kube.in_cluster()
        if args.role == "builder" and os.environ.get("KUBERNETES_SERVICE_HOST")
        else None
    )
    if not kube and args.role == "builder":
        logging.warning("not running in a Kubernetes cluster: tasks are acknowledged, not run")
    asyncio.run(Agent(args.url, args.token, kube, args.role, args.interval).run())


if __name__ == "__main__":
    main()
