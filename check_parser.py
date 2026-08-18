"""Scraper smoke test (nothing is sent to Discord)

  python check_parser.py            # fetch both categories live and print them
  python check_parser.py page.html  # parse a saved HTML file
"""
from __future__ import annotations

import json
import sys
import urllib.request

import config
from coconala import parse_requests


def show(source: str, html: str) -> None:
    jobs = parse_requests(html)
    print(f"\n=== {source}: {len(jobs)} postings ===")
    for job in jobs[:3]:
        print(
            json.dumps(
                {
                    "id": job.request_id,
                    "category": job.category,
                    "category_url": job.category_url,
                    "title": job.title,
                    "budget": job.budget_text,
                    "client": job.user_name,
                    "avatar": job.user_icon_url,
                    "profile": job.user_profile_url,
                    "posted": job.created_text,
                    "deadline": job.deadline_text,
                    "proposals": job.proposal_count,
                    "url": job.url,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    missing = [j.request_id for j in jobs if not j.user_icon_url]
    print(f"no avatar: {len(missing)}/{len(jobs)} (clients using the default icon)")


def main() -> None:
    if len(sys.argv) > 1:
        for path in sys.argv[1:]:
            with open(path, encoding="utf-8") as fh:
                show(path, fh.read())
        return

    for feed in config.FEEDS:
        req = urllib.request.Request(feed.url, headers={"User-Agent": config.USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            show(feed.url, resp.read().decode("utf-8"))


if __name__ == "__main__":
    main()
