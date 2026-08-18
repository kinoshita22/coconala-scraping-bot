"""Delivery test (this really does post one message per channel)

Sends the newest posting of each category to every configured destination
(Discord and/or Slack). state.json is left untouched, so real notifications
are unaffected.

  python send_test.py            # all configured destinations
  python send_test.py slack      # only Slack
  python send_test.py discord    # only Discord
"""
from __future__ import annotations

import asyncio
import logging
import sys

import aiohttp

import config
from coconala import parse_requests
from notifier import Notifier


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    only = sys.argv[1].lower() if len(sys.argv) > 1 else None
    if only and only not in (config.DISCORD, config.SLACK):
        raise SystemExit(f"unknown destination '{only}' (use discord or slack)")

    feeds = config.enabled_feeds()
    if not feeds:
        raise SystemExit("No webhook URL configured (check your .env)")

    timeout = aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        notifier = Notifier(session)
        await notifier.validate(feeds)

        for feed in feeds:
            destinations = [d for d in feed.destinations() if not only or d.kind == only]
            if not destinations:
                continue
            async with session.get(
                feed.url, headers={"User-Agent": config.USER_AGENT}
            ) as resp:
                jobs = parse_requests(await resp.text())
            if not jobs:
                print(f"{feed.key}: could not fetch any postings")
                continue
            for dest in destinations:
                ok = await notifier.send(feed, dest, jobs[0])
                print(f"{feed.key}/{dest.kind}: {'sent' if ok else 'FAILED'} -> {jobs[0].url}")


if __name__ == "__main__":
    asyncio.run(main())
