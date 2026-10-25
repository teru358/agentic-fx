"""Shared stubs for the news collector tests (no real network, no real rag)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from agentic_fx.activity import ActivityLog
from agentic_fx.datafeed.fetchers import Article
from agentic_fx.datafeed.news_collector import NewsCollector
from agentic_fx.store import news_sources
from agentic_fx.store.db import connect, init_db

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
HALF_HOUR = timedelta(minutes=30)


class MutableClock:
    def __init__(self, start: datetime = T0) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, delta: timedelta) -> None:
        self.t += delta


class FakeRag:
    """Counts what the collector hands over; same call surface as Rag."""

    def __init__(self) -> None:
        self.added: list[list[dict]] = []

    def add_news(self, articles, now) -> int:
        self.added.append(list(articles))
        return len(articles)

    def cleanup_news(self, now, hours=48) -> int:
        return 0


def article(n: int = 1, source: str = "s") -> Article:
    return Article(url=f"https://example.invalid/a{n}", title="t", body="b",
                   published=None, source_name=source)


def raised_status(code: int, headers: dict | None = None,
                  url: str = "https://example.invalid/feed") -> httpx.HTTPStatusError:
    """The exception raise_for_status gives for a response of this shape."""
    request = httpx.Request("GET", url)
    response = httpx.Response(code, headers=headers or {}, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("status below 400 does not raise")


@dataclass
class Scripted:
    """A fetch stub: outcomes are consumed in order, the last one repeats.

    An outcome is a list of articles, an exception to raise, or a callable
    taking the current time and returning one of those.
    """
    outcomes: list
    clock: MutableClock
    calls: list[datetime] = field(default_factory=list)

    def __call__(self, url, source_name, *, timeout_sec):
        self.calls.append(self.clock.now())
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if callable(outcome):
            outcome = outcome(self.clock.now())
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_env(tmp_path, *, rag=None, clock: MutableClock | None = None,
             db_name: str = "t.db"):
    clock = clock or MutableClock()
    conn = connect(tmp_path / db_name)
    init_db(conn)
    rag = rag if rag is not None else FakeRag()
    collector = NewsCollector(conn, rag, ActivityLog(tmp_path / "a.log"), clock,
                              timeout_sec=10)
    return conn, rag, clock, collector


def add_source(conn, name="src", fetcher="feed", url="https://example.invalid/feed",
               enabled=True) -> int:
    return news_sources.add(conn, name=name, fetcher=fetcher, url=url,
                            added_by="user", now=T0, enabled=enabled)


def run_grid(monkeypatch, collector, clock: MutableClock, fetch, *, hours: float,
             fetcher_attr: str = "fetch_feed") -> list[datetime]:
    """Run a cycle every 30 minutes from the clock's start through `hours`; return fetch times."""
    end = clock.now() + timedelta(hours=hours)
    with monkeypatch.context() as patch:
        patch.setattr(f"agentic_fx.datafeed.news_collector.{fetcher_attr}", fetch)
        while clock.now() <= end:
            collector.collect()
            clock.advance(HALF_HOUR)
    return list(fetch.calls)


def hours(*values: float) -> list[datetime]:
    return [T0 + timedelta(hours=v) for v in values]


def activity_lines(tmp_path) -> list[str]:
    path = tmp_path / "a.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def activity_details(tmp_path, kind: str) -> list[str]:
    """Detail field of every activity line of this kind."""
    return [line.split("\t")[3] for line in activity_lines(tmp_path)
            if line.split("\t")[2] == kind]


def route_httpx_get(patch, handler, seen: list | None = None) -> None:
    """Send httpx.get through a real httpx.Client on a MockTransport.

    The request still passes through the client's own logging and header
    handling; nothing leaves the process.  `patch` is a monkeypatch.context().
    """
    def get(url, **kwargs):
        def record(request):
            if seen is not None:
                seen.append(request)
            return handler(request)
        with httpx.Client(transport=httpx.MockTransport(record)) as client:
            return client.get(url, **kwargs)

    patch.setattr(httpx, "get", get)


RSS_TEMPLATE = (
    "<?xml version='1.0' encoding='UTF-8'?><rss version='2.0'><channel><title>feed</title>"
    "{items}</channel></rss>")


def rss_item(link: str, *, title: str = "headline", pub: str | None = None,
             description: str = "summary text") -> str:
    date = f"<pubDate>{pub}</pubDate>" if pub else ""
    return (f"<item><title>{title}</title><link>{link}</link>"
            f"<description>{description}</description>{date}</item>")


def rss(*items: str) -> bytes:
    return RSS_TEMPLATE.format(items="".join(items)).encode("utf-8")


ARTICLE_HTML = (
    "<html><head><meta charset='utf-8'></head><body><article>"
    + "".join(f"<p>Paragraph {i}: the central bank kept its policy rate unchanged on Friday and "
              f"signalled that future decisions depend on incoming data about inflation, wages "
              f"and the labour market across the whole economy.</p>" for i in range(1, 9))
    + "</article></body></html>")
