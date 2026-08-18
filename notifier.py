"""Message delivery to Discord and Slack incoming webhooks"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

import config
from config import DISCORD, SLACK, Destination, Feed
from coconala import JobRequest

logger = logging.getLogger(__name__)

EMBED_TITLE_MAX = 256
EMBED_DESC_MAX = 4096
AUTHOR_NAME_MAX = 256
SLACK_TEXT_MAX = 2000


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------
def build_embed(job: JobRequest, feed: Feed) -> dict:
    category = _truncate(job.category, 900)
    if job.category_url:
        category = f"[{category}]({job.category_url})"

    fields = [
        {"name": "Category", "value": category, "inline": True},
        {"name": "Budget", "value": _truncate(job.budget_text, 1024), "inline": True},
    ]
    if job.proposal_count is not None:
        fields.append({"name": "Proposals", "value": str(job.proposal_count), "inline": True})
    fields.append({"name": "Posted", "value": job.created_text, "inline": True})
    if job.deadline_at:
        fields.append({"name": "Deadline", "value": job.deadline_text, "inline": True})
    fields.append({"name": "Client", "value": _truncate(job.user_name, 1024), "inline": True})

    author: dict[str, str] = {"name": _truncate(job.user_name, AUTHOR_NAME_MAX)}
    if job.user_icon_url:
        author["icon_url"] = job.user_icon_url
    if job.user_profile_url:
        author["url"] = job.user_profile_url

    embed: dict = {
        "title": _truncate(job.title, EMBED_TITLE_MAX),
        "url": job.url,
        "color": feed.color,
        "author": author,
        "fields": fields,
        "footer": {"text": feed.label},
    }
    if job.user_icon_url:
        embed["thumbnail"] = {"url": job.user_icon_url}
    if job.created_at:
        embed["timestamp"] = job.created_at.isoformat()
    if config.INCLUDE_DESCRIPTION and job.content:
        embed["description"] = _truncate(
            job.content, min(config.DESCRIPTION_MAX_CHARS, EMBED_DESC_MAX)
        )
    return embed


def build_discord_payload(job: JobRequest, feed: Feed) -> dict:
    return {
        "content": job.url,  # plain URL alongside the embed, easy to copy
        "embeds": [build_embed(job, feed)],
        "allowed_mentions": {"parse": []},
    }


# --------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------
def _slack_escape(text: str) -> str:
    """Slack mrkdwn requires these three characters to be escaped"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_link(url: str | None, text: str) -> str:
    text = _slack_escape(text)
    return f"<{url}|{text}>" if url else text


def build_slack_payload(job: JobRequest, feed: Feed) -> dict:
    header = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": _truncate(
                f"*{_slack_link(job.url, job.title)}*\n"
                f"{_slack_link(job.category_url, job.category)}",
                SLACK_TEXT_MAX,
            ),
        },
    }
    # The client avatar rides along as the section accessory
    if job.user_icon_url:
        header["accessory"] = {
            "type": "image",
            "image_url": job.user_icon_url,
            "alt_text": _slack_escape(job.user_name),
        }

    fields = [
        {"type": "mrkdwn", "text": f"*Budget*\n{_slack_escape(job.budget_text)}"},
        {
            "type": "mrkdwn",
            "text": f"*Client*\n{_slack_link(job.user_profile_url, job.user_name)}",
        },
    ]
    if job.proposal_count is not None:
        fields.append({"type": "mrkdwn", "text": f"*Proposals*\n{job.proposal_count}"})
    fields.append({"type": "mrkdwn", "text": f"*Posted*\n{job.created_text}"})
    if job.deadline_at:
        fields.append({"type": "mrkdwn", "text": f"*Deadline*\n{job.deadline_text}"})

    blocks: list[dict] = [header, {"type": "section", "fields": fields}]

    if config.INCLUDE_DESCRIPTION and job.content:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _truncate(
                        _slack_escape(job.content),
                        min(config.DESCRIPTION_MAX_CHARS, SLACK_TEXT_MAX),
                    ),
                },
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{feed.label} · {job.url}"}],
        }
    )

    return {
        # Plain-text fallback used in notifications and unsupported clients
        "text": _truncate(f"New job: {_slack_escape(job.title)}\n{job.url}", SLACK_TEXT_MAX),
        "attachments": [{"color": feed.color_hex, "blocks": blocks}],
    }


BUILDERS = {DISCORD: build_discord_payload, SLACK: build_slack_payload}


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------
class Notifier:
    """Serializes delivery per webhook and honours each service's rate limits"""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_allowed: dict[str, float] = {}

    async def validate(self, feeds: list[Feed]) -> None:
        """Report the configured destinations at startup (posts no message)"""
        for feed in feeds:
            for dest in feed.destinations():
                if dest.kind == DISCORD:
                    await self._validate_discord(feed, dest)
                else:
                    # Slack incoming webhooks reject anything but a real POST,
                    # so there is no way to verify one without posting.
                    logger.info("%s -> Slack webhook configured", feed.key)

    async def _validate_discord(self, feed: Feed, dest: Destination) -> None:
        try:
            async with self._session.get(dest.url) as resp:
                if resp.status == 200:
                    info = await resp.json(content_type=None)
                    logger.info(
                        "%s -> connected to Discord webhook '%s' (channel_id=%s)",
                        feed.key,
                        info.get("name"),
                        info.get("channel_id"),
                    )
                else:
                    logger.error(
                        "%s: Discord webhook URL looks invalid (HTTP %s). Check your .env",
                        feed.key,
                        resp.status,
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("%s: could not verify the Discord webhook: %s", feed.key, exc)

    async def send(self, feed: Feed, dest: Destination, job: JobRequest) -> bool:
        payload = BUILDERS[dest.kind](job, feed)
        lock = self._locks.setdefault(dest.url, asyncio.Lock())

        async with lock:
            loop = asyncio.get_running_loop()
            wait = self._next_allowed.get(dest.url, 0.0) - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)

            for attempt in range(4):
                try:
                    async with self._session.post(dest.url, json=payload) as resp:
                        if resp.status == 429:
                            retry_after = await self._retry_after(resp)
                            logger.warning(
                                "rate limited by %s: waiting %.2fs (%s)",
                                dest.kind,
                                retry_after,
                                feed.key,
                            )
                            await asyncio.sleep(retry_after + 0.25)
                            continue
                        if resp.status >= 400:
                            text = await resp.text()
                            logger.error(
                                "failed to post to %s status=%s body=%s",
                                dest.kind,
                                resp.status,
                                text[:400],
                            )
                            return False
                        self._next_allowed[dest.url] = loop.time() + config.min_interval(
                            dest.kind
                        )
                        return True
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.warning(
                        "error posting to %s (attempt %d): %s", dest.kind, attempt + 1, exc
                    )
                    await asyncio.sleep(1.5 * (attempt + 1))
            logger.error("giving up on posting to %s: %s", dest.kind, job.url)
            return False

    @staticmethod
    async def _retry_after(resp: aiohttp.ClientResponse) -> float:
        """Discord returns retry_after in the JSON body, Slack in a header"""
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        try:
            body = await resp.json(content_type=None)
            return float((body or {}).get("retry_after", 1.0))
        except (aiohttp.ClientError, ValueError, TypeError):
            return 1.0
