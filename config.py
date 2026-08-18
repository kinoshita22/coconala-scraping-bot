"""Configuration (loaded from environment variables / .env)"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DISCORD = "discord"
SLACK = "slack"


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Destination:
    """One chat channel a feed is delivered to"""

    kind: str  # DISCORD or SLACK
    url: str  # incoming webhook URL
    state_key: str  # key used in the state file (independent per destination)


@dataclass(frozen=True)
class Feed:
    """One Coconala category -> one channel per chat service"""

    key: str  # identifier used in the state file
    label: str  # name shown in the message footer
    url: str  # listing page to scrape
    discord_webhook_url: str  # Discord webhook of the destination channel
    slack_webhook_url: str  # Slack incoming webhook of the destination channel
    color: int  # accent color

    @property
    def color_hex(self) -> str:
        return f"#{self.color:06x}"

    def destinations(self) -> list[Destination]:
        """Only the chat services that have a webhook configured"""
        out: list[Destination] = []
        if self.discord_webhook_url:
            # Discord keeps the bare feed key, so existing state files stay valid
            out.append(Destination(DISCORD, self.discord_webhook_url, self.key))
        if self.slack_webhook_url:
            out.append(Destination(SLACK, self.slack_webhook_url, f"{self.key}:slack"))
        return out


COCONALA_ORIGIN = "https://coconala.com"

FEEDS: list[Feed] = [
    Feed(
        key="category_22",
        label="Category 22 - Web design & development",
        url=f"{COCONALA_ORIGIN}/requests/categories/22",
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_CATEGORY_22", ""),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_CATEGORY_22", ""),
        color=0x00B4A0,
    ),
    Feed(
        key="category_11",
        label="Category 11 - IT & programming",
        url=f"{COCONALA_ORIGIN}/requests/categories/11",
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_CATEGORY_11", ""),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_CATEGORY_11", ""),
        color=0x4A7DFF,
    ),
]

# Polling interval in seconds
POLL_INTERVAL = _env_float("POLL_INTERVAL", 3.0)

# HTTP timeout in seconds
HTTP_TIMEOUT = _env_float("HTTP_TIMEOUT", 20.0)

# Notify every existing posting on the very first run
# (False = record them as already seen without sending)
NOTIFY_ON_FIRST_RUN = _env_bool("NOTIFY_ON_FIRST_RUN", False)

# Max notifications sent per polling cycle
MAX_NOTIFY_PER_CYCLE = int(os.getenv("MAX_NOTIFY_PER_CYCLE", "10"))

# Include an excerpt of the job description in the message
INCLUDE_DESCRIPTION = _env_bool("INCLUDE_DESCRIPTION", False)
DESCRIPTION_MAX_CHARS = int(os.getenv("DESCRIPTION_MAX_CHARS", "300"))

# Where seen request IDs are stored, and how many to keep
STATE_FILE = Path(os.getenv("STATE_FILE", BASE_DIR / "state.json"))
STATE_MAX_IDS = int(os.getenv("STATE_MAX_IDS", "2000"))

# Minimum gap between two posts to the same webhook, in seconds
DISCORD_MIN_INTERVAL = _env_float("DISCORD_MIN_INTERVAL", 1.2)
SLACK_MIN_INTERVAL = _env_float("SLACK_MIN_INTERVAL", 1.2)

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def min_interval(kind: str) -> float:
    return SLACK_MIN_INTERVAL if kind == SLACK else DISCORD_MIN_INTERVAL


def enabled_feeds() -> list[Feed]:
    """Return only the feeds that have at least one destination configured"""
    return [f for f in FEEDS if f.destinations()]
