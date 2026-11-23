import argparse
import asyncio
import logging
import os

from .client import Agent


def main():
    parser = argparse.ArgumentParser(prog="kaji-agent", description="Run kaji builds on this node.")
    parser.add_argument("--url", default=os.environ.get("KAJI_URL", "wss://console.kaji.systems/agent/ws"))
    parser.add_argument("--token", default=os.environ.get("KAJI_TOKEN", ""), help="the builder's token (or KAJI_TOKEN)")
    args = parser.parse_args()
    if not args.token:
        parser.error("a token is needed: --token or KAJI_TOKEN")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(Agent(args.url, args.token).run())


if __name__ == "__main__":
    main()
