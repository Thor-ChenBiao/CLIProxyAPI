#!/usr/bin/env python3
"""Run one local CLIProxyAPI usage subscriber."""

import argparse
import os
import signal
import threading

from auth_usage_collector import AuthUsageCollector
from auth_usage_store import AuthUsageStore


def build_collector(node_name, node_url, database_url, management_key, flush_seconds=2):
    store = AuthUsageStore(database_url)
    collector = AuthUsageCollector(
        store=store,
        nodes=[{"name": node_name, "url": node_url}],
        management_key=management_key,
        flush_seconds=flush_seconds,
    )
    return store, collector


def main():
    parser = argparse.ArgumentParser(description="Persist local CLIProxyAPI auth usage aggregates")
    parser.add_argument("--node-name", required=True)
    parser.add_argument("--node-url", default="http://127.0.0.1:8317")
    parser.add_argument("--flush-seconds", type=float, default=2)
    args = parser.parse_args()

    database_url = os.environ.get("KEY_PORTAL_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    management_key = os.environ.get("CLIPROXY_MANAGEMENT_KEY", "admin123")
    store, collector = build_collector(
        node_name=args.node_name,
        node_url=args.node_url,
        database_url=database_url,
        management_key=management_key,
        flush_seconds=args.flush_seconds,
    )
    if not store.ensure_schema():
        raise RuntimeError("KEY_PORTAL_DATABASE_URL or DATABASE_URL is required")

    stopped = threading.Event()

    def stop(*_args):
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    collector.start()
    print(f"[AuthUsage] Agent started for {args.node_name}")
    stopped.wait()
    collector.stop()


if __name__ == "__main__":
    main()
