"""Long-running bot that watches Coconala for new job requests and posts them to chat

  python main.py
"""
from __future__ import annotations

import asyncio
import logging
import signal

import aiohttp

import config
from coconala import JobRequest, parse_requests
from config import Destination
from notifier import Notifier
from state import SeenStore

logger = logging.getLogger("coconala-bot")

# How often to log a liveness line, in seconds
HEARTBEAT_INTERVAL = 600


class FeedWatcher:
    def __init__(
        self,
        feed: config.Feed,
        session: aiohttp.ClientSession,
        notifier: Notifier,
        store: SeenStore,
    ) -> None:
        self.feed = feed
        self.session = session
        self.notifier = notifier
        self.store = store
        self._failures = 0
        self._tick = 0
        self.latest: JobRequest | None = None  # newest posting, for the heartbeat log

    def set_tick(self, tick: int) -> None:
        self._tick = tick

    async def _fetch(self) -> str | None:
        headers = {
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        }
        try:
            async with self.session.get(self.feed.url, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("%s: HTTP %s", self.feed.key, resp.status)
                    return None
                return await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("%s: fetch failed: %s", self.feed.key, exc)
            return None

    async def poll_once(self) -> None:
        # Back off while failures persist (skip this cycle entirely)
        if self._failures:
            skip = min(2 ** (self._failures - 1), 20)
            if self._tick % (skip + 1):
                return

        html = await self._fetch()
        if html is None:
            self._failures = min(self._failures + 1, 6)
            return

        try:
            jobs = parse_requests(html)
        except Exception:  # never let a parse error kill the bot
            logger.exception("%s: parsing failed", self.feed.key)
            self._failures = min(self._failures + 1, 6)
            return

        if not jobs:
            logger.debug("%s: listing returned 0 postings", self.feed.key)
            self._failures = min(self._failures + 1, 6)
            return
        self._failures = 0
        self.latest = jobs[0]

        # Each destination tracks its own delivery state, so a failure or a newly
        # added chat service never affects the other one.
        for dest in self.feed.destinations():
            await self._deliver(dest, jobs)

    async def _deliver(self, dest: Destination, jobs: list[JobRequest]) -> None:
        first_run = not self.store.is_known(dest.state_key)
        if first_run and not config.NOTIFY_ON_FIRST_RUN:
            self.store.add_many(dest.state_key, [j.request_id for j in reversed(jobs)])
            self.store.save()
            logger.info(
                "%s/%s: first run, marked %d existing postings as seen",
                self.feed.key,
                dest.kind,
                len(jobs),
            )
            return

        new_jobs = [j for j in jobs if not self.store.has(dest.state_key, j.request_id)]
        if not new_jobs:
            return

        # Send oldest first so the channel reads chronologically
        new_jobs.sort(key=lambda j: (j.created_at.timestamp() if j.created_at else 0, j.request_id))

        if len(new_jobs) > config.MAX_NOTIFY_PER_CYCLE:
            logger.warning(
                "%s/%s: %d new postings, skipping the %d oldest (marked as seen)",
                self.feed.key,
                dest.kind,
                len(new_jobs),
                len(new_jobs) - config.MAX_NOTIFY_PER_CYCLE,
            )
            for job in new_jobs[: -config.MAX_NOTIFY_PER_CYCLE]:
                self.store.add(dest.state_key, job.request_id)
            new_jobs = new_jobs[-config.MAX_NOTIFY_PER_CYCLE :]

        for job in new_jobs:
            if await self.notifier.send(self.feed, dest, job):
                # Only mark as seen once delivered, so failures are retried next cycle
                self.store.add(dest.state_key, job.request_id)
                logger.info("[%s/%s] %s | %s", self.feed.key, dest.kind, job.title, job.url)
        self.store.save()


async def run() -> None:
    feeds = config.enabled_feeds()
    if not feeds:
        raise SystemExit(
            "No webhook URL configured. Set DISCORD_WEBHOOK_CATEGORY_* and/or "
            "SLACK_WEBHOOK_CATEGORY_* in your .env file."
        )
    for feed in config.FEEDS:
        if not feed.destinations():
            logger.warning("%s: no webhook URL configured, skipping", feed.key)

    store = SeenStore(config.STATE_FILE, config.STATE_MAX_IDS)
    timeout = aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT)
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        notifier = Notifier(session)
        await notifier.validate(feeds)
        watchers = [FeedWatcher(f, session, notifier, store) for f in feeds]

        logger.info(
            "watching for new postings (every %.1fs, %d feeds, %d destinations)",
            config.POLL_INTERVAL,
            len(watchers),
            sum(len(f.destinations()) for f in feeds),
        )
        tick = 0
        heartbeat = loop.time()
        while not stop.is_set():
            started = loop.time()
            for w in watchers:
                w.set_tick(tick)
            results = await asyncio.gather(
                *(w.poll_once() for w in watchers), return_exceptions=True
            )
            for w, r in zip(watchers, results):
                if isinstance(r, Exception):
                    logger.exception("%s: unexpected error", w.feed.key, exc_info=r)

            tick += 1
            # Liveness line, so a quiet channel is distinguishable from a dead bot
            if loop.time() - heartbeat >= HEARTBEAT_INTERVAL:
                heartbeat = loop.time()
                logger.info(
                    "still watching (nothing new): %s",
                    " / ".join(
                        f"{w.feed.key} newest={w.latest.request_id}@{w.latest.created_text}"
                        if w.latest
                        else f"{w.feed.key} fetch failed"
                        for w in watchers
                    ),
                )

            elapsed = loop.time() - started
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=max(0.0, config.POLL_INTERVAL - elapsed)
                )
            except asyncio.TimeoutError:
                pass

        store.save()
        logger.info("stopped")


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
