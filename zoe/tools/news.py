"""Current news retrieval over public RSS feeds.

Why RSS and not a search API: the project already depends on ``requests`` and
nothing else, and every feed here is public and needs no credential. A search API
would mean a new dependency, a new key and a new failure mode for a problem that
a feed solves outright. Feeds also carry the thing this module exists to
preserve: **a publication date the publisher asserted**, which is what lets a
caller tell today's news from a two-year-old article about the same topic.

Two rules shape everything below.

* **Never invent.** A result without a date is undated, not fresh, and is
  labelled that way rather than being given a date. No result means no results,
  said plainly. A failure is reported as a failure.
* **Freshness is data, not a filter.** Results are ordered and labelled by
  publication date; nothing is discarded for being old, because background
  material is still useful when it says so.
"""

from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import List, Optional

import requests

from zoe.config import NEWS_TIMEOUT_S, USER_AGENT
from zoe.logger import logger

#: Publishers whose own announcements are the primary source for space news.
#: Journalism is kept alongside them rather than instead of them: the agencies
#: announce, the journalists report and analyse.
_SOURCES = {
    "NASA": "https://www.nasa.gov/news-release/feed/",
    "SpaceNews": "https://spacenews.com/feed/",
    "ESA": "https://www.esa.int/rssfeed/Our_Activities/Space_News",
}

#: How many items to keep per feed. Enough to cover a few days of a slow
#: publisher without filling the model's context with a month of backlog.
_ITEMS_PER_FEED = 8

#: Never follow a feed to another host: the URLs are ours, not user input.
_ALLOWED_HOSTS = {url.split("/")[2] for url in _SOURCES.values()}


def _now(clock=None) -> datetime.datetime:
    return (clock or datetime.datetime.now)(datetime.timezone.utc)


def _parse_date(raw: str) -> Optional[datetime.datetime]:
    """Publisher's publication date, or None. Never guessed."""
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def _text(node, *tags: str) -> str:
    for tag in tags:
        found = node.find(tag)
        if found is not None and (found.text or "").strip():
            return (found.text or "").strip()
    return ""


@dataclass
class NewsItem:
    """One retrieved article, with the metadata a freshness claim needs."""

    title: str
    url: str = ""
    source: str = ""
    #: When the publisher says it published this. None means unknown, which is
    #: uncertainty and must never be rendered as a date.
    published: Optional[datetime.datetime] = None
    summary: str = ""
    #: When we retrieved it, always known.
    retrieved_at: Optional[datetime.datetime] = None
    #: True when the publisher's date is within `window_days` of retrieval.
    is_fresh: bool = False
    #: True when `published` is None: use it as background, never as news.
    is_undated: bool = False

    @property
    def age_days(self) -> Optional[int]:
        if self.published is None or self.retrieved_at is None:
            return None
        return (self.retrieved_at.date() - self.published.date()).days

    def label(self) -> str:
        """How this item may be described. Never claims currency it lacks."""
        if self.is_undated:
            return "publication date unknown"
        age = self.age_days
        if age is None:
            return "publication date unknown"
        if age <= 0:
            return "today"
        if age == 1:
            return "yesterday"
        if age < 7:
            return f"{age} days ago"
        if age < 60:
            return f"{age // 7} weeks ago"
        return f"{age // 30} months ago"


@dataclass
class NewsResult:
    """Everything one retrieval produced, including how it went."""

    items: List[NewsItem] = field(default_factory=list)
    retrieved_at: Optional[datetime.datetime] = None
    #: Populated when a feed failed. A partial result carries its failures so the
    #: caller can be honest about completeness rather than assuming it.
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.items)

    @property
    def fresh(self) -> List[NewsItem]:
        return [item for item in self.items if item.is_fresh]

    @property
    def background(self) -> List[NewsItem]:
        return [item for item in self.items if not item.is_fresh]


def fetch_news(
    topic: str = "space",
    *,
    window_days: int = 3,
    clock=None,
    session=None,
) -> NewsResult:
    """Fetch recent articles from the configured feeds.

    Never raises. Returns whatever came back plus the failures, so a caller can
    never mistake a broken feed for an empty news day.

    Args:
        topic: Reserved for narrowing feeds that support it. All configured
            feeds are space sources, so this does not currently filter.
        window_days: Age at or below which an item counts as fresh.
        clock: Callable returning the current UTC time. Injected in tests.
        session: Object with a `.get(url, ...)` method. Injected in tests.
    """
    now = _now(clock)
    result = NewsResult(retrieved_at=now)
    get = (session or requests).get

    for name, url in _SOURCES.items():
        if url.split("/")[2] not in _ALLOWED_HOSTS:
            continue
        try:
            response = get(url, timeout=NEWS_TIMEOUT_S,
                           headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            items = _parse_feed(name, response.content, now, window_days)
        except Exception as e:  # noqa: BLE001 -- one dead feed is not a dead tool
            logger.warning("News feed %s failed: %s", name, e)
            result.errors.append(f"{name}: {e}")
            continue
        if not items:
            result.errors.append(f"{name}: no items parsed")
            continue
        result.items.extend(items)

    # Newest first; undated last, because an unknown date is not evidence of
    # recency. Ties broken by title so the ordering is deterministic.
    result.items.sort(
        key=lambda item: (item.published is None,
                          -(item.published.timestamp() if item.published else 0),
                          item.title)
    )
    logger.info(
        "[NEWS] topic=%s items=%d fresh=%d errors=%d window=%dd",
        topic, len(result.items), len(result.fresh), len(result.errors), window_days,
    )
    return result


def _parse_feed(source: str, payload: bytes, now: datetime.datetime,
                window_days: int) -> List[NewsItem]:
    """Turn one feed body into items. A malformed feed yields [], not an error."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as e:
        logger.warning("News feed %s is not valid XML: %s", source, e)
        return []

    found: List[NewsItem] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "item":
            continue
        title = _text(node, "title", "{http://www.w3.org/2005/Atom}title")
        if not title:
            continue
        published = _parse_date(_text(node, "pubDate", "{http://purl.org/dc/elements/1.1/}date"))
        age = None if published is None else (now.date() - published.date()).days
        found.append(NewsItem(
            title=title,
            url=_text(node, "link"),
            source=source,
            published=published,
            summary=_text(
            node, "description",
            "{http://www.w3.org/2005/Atom}summary",
            "{http://purl.org/rss/1.0/modules/content/}encoded",
        ),
            retrieved_at=now,
            is_fresh=age is not None and 0 <= age <= window_days,
            is_undated=published is None,
        ))
        if len(found) >= _ITEMS_PER_FEED:
            break
    return found


def format_news(result: NewsResult, *, limit: int = 3) -> str:
    """Tool output for the model: fresh items first, background clearly marked.

    Written for grounding, not for reading aloud: no URLs in the spoken text, no
    JSON, and every item carries its source and date so the model can attribute
    rather than guess. When there is nothing fresh, that is stated outright
    instead of being papered over with old items.
    """
    if not result.ok:
        detail = "; ".join(result.errors) if result.errors else "no items returned"
        return (f"NEWS RETRIEVAL FAILED: no articles were retrieved ({detail}). "
                "Do not answer from memory and do not describe anything as current.")

    fresh = result.fresh[:limit]
    lines = [
        f"NEWS RETRIEVAL SUCCEEDED: {len(result.items)} articles from "
        f"{len(result.fresh)} within the freshness window, retrieved "
        f"{result.retrieved_at:%Y-%m-%d %H:%M UTC}.",
        "These are the most recent items. Use only what is listed here for "
        "claims about current events.",
        "",
    ]
    for item in fresh:
        lines.append(f"- [{item.source} | {item.label()}] {item.title}")
        if item.summary:
            lines.append(f"    {item.summary.strip()}")
        if item.url:
            lines.append(f"    source_url: {item.url}")

    background = result.background
    if background:
        lines.append("")
        lines.append(
            f"BACKGROUND ONLY ({len(background)} older or undated items — NOT "
            "current news, do not present these as today's events):"
        )
        for item in background[:3]:
            lines.append(f"- [{item.source} | {item.label()}] {item.title}")
        if any(item.is_undated for item in background):
            lines.append(
                "- Some items have no publication date. Their age is unknown, "
                "which is not the same as being recent."
            )

    if result.errors:
        lines.append("")
        lines.append(
            f"Note: {len(result.errors)} feed(s) failed, so this list may be "
            f"incomplete: {'; '.join(result.errors)}"
        )
    return "\n".join(lines)


def search_news(query: str = "", *, window_days: int = 3, clock=None,
                session=None) -> str:
    """Fetch current news and return it as source-attributed text for the model.

    Use this for ANY question about what is happening now -- today's news, the
    latest developments, a recent update, what happened this week. Do NOT use it
    for background explanation ("what is a black hole", "how do rockets work"),
    which you can answer directly.

    Args:
        query: Optional topic words, e.g. "Artemis" or "SpaceX". Leave empty for
            general current news.
        window_days: How recent counts as current. Defaults to 3 days.

    Returns:
        Formatted results with source and publication date for each item, or an
        explicit failure message. Never invents a result, a date or a URL.
    """
    result = fetch_news(" ".join((query or "").split()) or "space",
                        window_days=window_days, clock=clock, session=session)
    return format_news(result)


__all__ = ["NewsItem", "NewsResult", "fetch_news", "format_news", "search_news"]
