"""Scraper for Coconala job-request listing pages

The listing is server-rendered by Nuxt, and the full request list is embedded in
`window.__NUXT__` as structured data. Values are minified into variable
references (e.g. `minBudget:B`), so we rebuild them by pairing the IIFE's
parameter names with its argument list before converting the object to JSON.

If the Nuxt payload format ever changes, we fall back to parsing the rendered
HTML cards (in that case the client avatar is not available).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9), "JST")
ORIGIN = "https://coconala.com"

_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUXT_HEAD_RE = re.compile(r"window\.__NUXT__=\(function\(([^)]*)\)\{")


@dataclass
class JobRequest:
    request_id: int
    title: str
    url: str
    category: str  # displayed category name (the subcategory)
    category_url: str | None  # e.g. https://coconala.com/requests/categories/236
    budget_text: str
    user_id: int
    user_name: str
    user_icon_url: str | None
    user_profile_url: str | None
    content: str
    proposal_count: int | None
    created_at: datetime | None
    deadline_at: datetime | None

    @property
    def created_text(self) -> str:
        return (
            self.created_at.strftime("%Y/%m/%d %H:%M") if self.created_at else "Unknown"
        )

    @property
    def deadline_text(self) -> str:
        return self.deadline_at.strftime("%Y/%m/%d") if self.deadline_at else "Unknown"


class ParseError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Rebuilding the Nuxt payload
# --------------------------------------------------------------------------
def _split_top_level_args(text: str) -> list[str]:
    """Split on top-level commas while respecting string literals"""
    args: list[str] = []
    buf: list[str] = []
    in_str = False
    escaped = False
    for ch in text:
        if in_str:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            buf.append(ch)
        elif ch == ",":
            args.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    args.append("".join(buf))
    return args


def _extract_nuxt_script(html: str) -> str:
    start = html.find("window.__NUXT__")
    if start < 0:
        raise ParseError("window.__NUXT__ not found")
    end = html.find("</script>", start)
    if end < 0:
        raise ParseError("end of the __NUXT__ script not found")
    return html[start:end]


def _build_var_table(script: str) -> dict[str, str]:
    """Map each minified variable name to its JS literal"""
    m = _NUXT_HEAD_RE.match(script)
    if not m:
        raise ParseError("could not parse the __NUXT__ IIFE header")
    params = [p.strip() for p in m.group(1).split(",") if p.strip()]

    call = script.rfind("}(")
    if call < 0:
        raise ParseError("argument list of the __NUXT__ IIFE not found")
    tail = script[call + 2 :].strip().rstrip(";").rstrip(")")
    values = _split_top_level_args(tail)

    if len(params) != len(values):
        # If they don't line up, the mapping cannot be trusted
        raise ParseError(f"argument count mismatch: params={len(params)} args={len(values)}")
    return dict(zip(params, values))


def _slice_balanced(text: str, start: int) -> str:
    """Slice from the `[` / `{` at `start` up to its matching closing bracket"""
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ParseError("unbalanced brackets")


def _js_object_to_json(src: str, table: dict[str, str]) -> Any:
    """Convert a JS object literal (bare keys, variable refs) into JSON"""
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    break
                j += 1
            out.append(src[i : j + 1])
            i = j + 1
            continue

        m = _IDENT_RE.match(src, i)
        if m:
            word = m.group(0)
            end = m.end()
            if end < n and src[end] == ":":
                out.append(f'"{word}":')  # bare key -> quoted key
                i = end + 1
            else:
                out.append(table.get(word, "null"))  # variable ref -> actual value
                i = end
            continue

        out.append(ch)
        i += 1
    return json.loads("".join(out))


def _extract_request_list(html: str) -> list[dict[str, Any]]:
    script = _extract_nuxt_script(html)
    table = _build_var_table(script)

    key = script.find("requestsList:[")
    if key < 0:
        raise ParseError("requestsList not found")
    array_src = _slice_balanced(script, script.index("[", key))
    data = _js_object_to_json(array_src, table)
    if not isinstance(data, list):
        raise ParseError("requestsList is not an array")
    return data


# --------------------------------------------------------------------------
# HTML side (the category display name only exists in the rendered HTML)
# --------------------------------------------------------------------------
def _category_of(card) -> tuple[str, str | None]:
    """Return (category name, category URL) for a listing card"""
    cat = card.select_one("a.c-itemInfo_category")
    if not cat:
        return "Unknown", None
    href = cat.get("href") or ""
    url = f"{ORIGIN}{href}" if href.startswith("/") else (href or None)
    return cat.get_text(strip=True), url


def _category_map(soup: BeautifulSoup) -> dict[int, tuple[str, str | None]]:
    """requestId -> (category name, category URL)"""
    mapping: dict[int, tuple[str, str | None]] = {}
    for card in soup.select("div.c-searchItemWrapper"):
        link = card.select_one("a.c-searchItem_detailLink[href]")
        if not link:
            continue
        m = re.search(r"/requests/(\d+)", link["href"])
        if m:
            mapping[int(m.group(1))] = _category_of(card)
    return mapping


def _parse_html_cards(soup: BeautifulSoup) -> list[JobRequest]:
    """Fallback used when the Nuxt payload cannot be read (no avatars available)"""
    jobs: list[JobRequest] = []
    for card in soup.select("div.c-searchItemWrapper"):
        link = card.select_one("a.c-searchItem_detailLink[href]")
        title_el = card.select_one(".c-itemInfo_title a")
        if not link or not title_el:
            continue
        m = re.search(r"/requests/(\d+)", link["href"])
        if not m:
            continue
        request_id = int(m.group(1))

        cat_name, cat_url = _category_of(card)
        name_el = card.select_one(".c-itemInfoUser_name")
        desc_el = card.select_one(".c-itemInfo_description")

        budget = "Unknown"
        proposals = None
        for tile in card.select("div.c-itemTile_tile"):
            classes = tile.get("class") or []
            content = tile.select_one(".c-itemTileContent")
            text = re.sub(r"\s+", "", content.get_text(" ", strip=True)) if content else ""
            if "c-itemTile_tile-budget" in classes:
                budget = text or "Unknown"
            elif "c-itemTile_tile-proposalCount" in classes and text.isdigit():
                proposals = int(text)

        created = None
        created_el = card.select_one(".c-itemInfoUser_created span[title]")
        if created_el:
            dm = re.search(r"(\d+)年(\d+)月(\d+)日.*?(\d+):(\d+)", created_el["title"])
            if dm:
                y, mo, d, hh, mm = (int(x) for x in dm.groups())
                created = datetime(y, mo, d, hh, mm, tzinfo=JST)

        jobs.append(
            JobRequest(
                request_id=request_id,
                title=title_el.get_text(strip=True),
                url=f"{ORIGIN}/requests/{request_id}",
                category=cat_name,
                category_url=cat_url,
                budget_text=budget,
                user_id=0,
                user_name=name_el.get_text(strip=True) if name_el else "Unknown",
                user_icon_url=None,
                user_profile_url=None,
                content=desc_el.get_text("\n", strip=True) if desc_el else "",
                proposal_count=proposals,
                created_at=created,
                deadline_at=None,
            )
        )
    return jobs


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def _format_budget(entry: dict[str, Any]) -> str:
    if entry.get("budgetConsultation"):
        return "Negotiable"
    lo = entry.get("minBudget") or 0
    hi = entry.get("maxBudget") or 0
    if lo and hi:
        return f"¥{lo:,}" if lo == hi else f"¥{lo:,} - ¥{hi:,}"
    if hi:
        return f"up to ¥{hi:,}"
    if lo:
        return f"from ¥{lo:,}"
    return "Negotiable"


def _to_datetime(ts: Any) -> datetime | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=JST)


def parse_requests(html: str) -> list[JobRequest]:
    """Extract the job requests from a listing page (newest first)"""
    soup = BeautifulSoup(html, "html.parser")

    try:
        entries = _extract_request_list(html)
    except (ParseError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Nuxt payload could not be parsed, falling back to HTML: %s", exc)
        return _parse_html_cards(soup)

    categories = _category_map(soup)
    jobs: list[JobRequest] = []
    for entry in entries:
        request_id = entry.get("requestId")
        if not isinstance(request_id, int) or entry.get("isDeleted"):
            continue

        icon_path = entry.get("userIconPath") or ""
        user_id = entry.get("userId") or 0
        cat_name, cat_url = categories.get(request_id, ("Unknown", None))

        jobs.append(
            JobRequest(
                request_id=request_id,
                title=entry.get("title") or "(untitled)",
                url=f"{ORIGIN}/requests/{request_id}",
                category=cat_name,
                category_url=cat_url,
                budget_text=_format_budget(entry),
                user_id=user_id,
                user_name=entry.get("userName") or "Unknown",
                user_icon_url=f"{ORIGIN}{icon_path}" if icon_path else None,
                user_profile_url=f"{ORIGIN}/users/{user_id}" if user_id else None,
                content=entry.get("requestContent") or "",
                proposal_count=entry.get("proposalCount"),
                created_at=_to_datetime(entry.get("created")),
                deadline_at=_to_datetime(entry.get("recruitmentDeadline")),
            )
        )
    return jobs
