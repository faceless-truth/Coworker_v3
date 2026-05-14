"""One-shot missed-notification backfill CLI.

``python -m coworker.workers.backfill_missed``

Runs a single pass of ``sweep_missed_backfill`` and exits.
Designed to be invoked by the systemd timer
(``coworker-backfill.timer``) on a cadence well under the
window the missed-notification marker buys us — typically every
5 minutes. Each tick reconciles every subscription with
``last_missed_at`` set and clears the marker on success.

Exits 0 on success even when individual rows had errors; the
timer keeps trying. Logs a structured summary so dashboards can
alert on persistent failures.
"""
import argparse
import asyncio
import sys

from loguru import logger

from coworker.db import redis as redis_module
from coworker.db.session import get_sessionmaker
from coworker.graph.missed_sweep import sweep_missed_backfill
from coworker.logging import setup_logging
from coworker.workers.plugin_queue import PluginEventQueue


async def _amain() -> int:
    setup_logging()
    sm = get_sessionmaker()
    redis = redis_module.get_redis()
    queue = PluginEventQueue(redis)

    try:
        result = await sweep_missed_backfill(
            sessionmaker=sm, queue=queue,
        )
    finally:
        await redis.aclose()

    if result.firms_seen == 0:
        logger.info("missed backfill sweep no active firms")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one pass of the missed-notification backfill sweep. "
            "Designed to be called by a systemd timer."
        ),
    )
    parser.parse_args(argv)
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
