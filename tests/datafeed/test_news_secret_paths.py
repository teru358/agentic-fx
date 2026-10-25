"""A source URL can carry a secret; it must reach no output except the fetch itself."""
import dataclasses
import logging
import sqlite3
import warnings
from pathlib import Path
from datetime import timedelta

import httpx
import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import SystemClock
from agentic_fx.datafeed.news_collector import NewsCollector
from agentic_fx.logging_setup import setup_technical_logging
from agentic_fx.setup.pending_facts import collect_pending_facts
from agentic_fx.setup.pending_steps import pending_steps
from agentic_fx.setup.pending_text import render_pending_step
from agentic_fx.store import news_sources
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag
from tests.datafeed.news_support import (
    ARTICLE_HTML, HALF_HOUR, T0, MutableClock, activity_details, route_httpx_get, rss, rss_item,
)
from tests.store.test_rag import FakeEmbedding

Q = "ZQcanaryQ9xk"
P = "ZPcanaryP7mw"
BASE = f"https://example.invalid/{P}"
V6 = "https://[2001:db8::1]"


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    leaky = f"failure at {url}"
    last = request.url.path.rsplit("/", 1)[-1]
    if last == "feed-ok":
        return httpx.Response(200, content=rss(
            rss_item("https://example.invalid/story-1", pub="Wed, 29 Jul 2026 18:20:42 JST"),
            rss_item("https://example.invalid/story-2", pub="Wed, 29 Jul 2026 18:20:42 GMT"),
            rss_item("web:web-one"), rss_item("javascript:void(0)")))
    if last == "feed-cross":
        return httpx.Response(200, content=rss(rss_item(f"{BASE}/web-same"),
                                               rss_item("web:web-one")))
    if last == "v6-ok":
        return httpx.Response(200, content=rss(rss_item("https://example.invalid/v6-story")))
    if last in {"web-same", "web-second"}:
        return httpx.Response(200, content=ARTICLE_HTML.encode(),
                              headers={"content-type": "text/html; charset=utf-8"})
    if last == "feed-404":
        return httpx.Response(404, content=leaky.encode())
    if last == "feed-429":
        return httpx.Response(429, headers={"Retry-After": "60"}, content=leaky.encode())
    if last == "feed-503":
        return httpx.Response(503, content=leaky.encode())
    if last == "feed-timeout":
        raise httpx.ReadTimeout(leaky, request=request)
    if last == "feed-conn":
        raise httpx.ConnectError(leaky, request=request)
    if last == "v6-conn":
        raise httpx.ConnectError(leaky, request=request)
    if last == "feed-bad":
        return httpx.Response(200, content=b"<html><body>" + leaky.encode() + b"</body></html>")
    if last == "feed-boom":
        raise RuntimeError(f"unexpected {url} {leaky}")
    raise AssertionError(f"unrouted {url}")


SOURCES = [
    ("feed-ok", "feed", f"{BASE}/feed-ok?k={Q}"),
    ("feed-cross", "feed", f"{BASE}/feed-cross?k={Q}"),
    ("web-one", "web", f"{BASE}/web-same?k={Q}"),
    ("web-two", "web", f"{BASE}/web-same?k={Q}2"),
    ("v6-ok", "feed", f"{V6}/{P}/v6-ok?k={Q}"),
    ("e404", "feed", f"{BASE}/feed-404?k={Q}"),
    ("e429", "feed", f"{BASE}/feed-429?k={Q}"),
    ("e503", "feed", f"{BASE}/feed-503?k={Q}"),
    ("etimeout", "web", f"{BASE}/feed-timeout?k={Q}"),
    ("econn", "feed", f"{BASE}/feed-conn?k={Q}"),
    ("ev6", "feed", f"{V6}/{P}/v6-conn?k={Q}"),
    ("ebad", "feed", f"{BASE}/feed-bad?k={Q}"),
    ("eboom", "web", f"{BASE}/feed-boom?k={Q}"),
]


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))
        self.lines.append(record.getMessage())
        self.lines.append(repr(record.args))
        if record.exc_info:
            self.lines.append(repr(record.exc_info))


def _dump_tables(conn) -> str:
    out = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if table == "news_sources" or table.startswith("sqlite_"):
            continue
        out.append(table)
        out.extend(repr(tuple(row)) for row in conn.execute(f'SELECT * FROM "{table}"'))
    return "\n".join(out)


@pytest.fixture
def world(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    technical = logging.getLogger("agentic_fx")
    saved = (technical.level, technical.propagate, list(technical.handlers))
    setup_technical_logging(logs, "DEBUG")
    capture = Capture()
    root = logging.getLogger()
    root_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    clock = MutableClock()
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())
    for name, fetcher, url in SOURCES:
        news_sources.add(conn, name=name, fetcher=fetcher, url=url, added_by="user",
                         now=T0, enabled=True)
    collector = NewsCollector(conn, rag, ActivityLog(tmp_path / "a.log"), clock, timeout_sec=5)
    seen: list[httpx.Request] = []
    with monkeypatch.context() as patch:
        route_httpx_get(patch, _handler, seen)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                for _ in range(8):
                    collector.collect()
                    clock.advance(HALF_HOUR)
            finally:
                root.removeHandler(capture)
                root.setLevel(root_level)
                for h in list(technical.handlers):
                    technical.removeHandler(h)
                    h.close()
                technical.setLevel(saved[0])
                technical.propagate = saved[1]
                for h in saved[2]:
                    technical.addHandler(h)
    yield dict(tmp=tmp_path, conn=conn, rag=rag, capture=capture, caught=caught, seen=seen,
               tech=(logs / "agentic.log").read_text(encoding="utf-8"), clock=clock)
    conn.close()


def _status_text(conn):
    facts = collect_pending_facts(Path("/nonexistent-root"), None, conn, deployed_plugins=(), outage=None, clock=SystemClock(),
                                  landlock_available=True, landlock_abi=8, service_active=True)
    facts = dataclasses.replace(facts, record_state="ok", record=None)
    steps = pending_steps(facts, for_status=True)
    from agentic_fx.service import _serialize_status_setup
    return ("\n".join(render_pending_step(s) for s in steps) + "\n" + repr(_serialize_status_setup(facts)),
            steps)


def test_secret_query_reaches_no_output(world):
    w = world
    activity = (w["tmp"] / "a.log").read_text(encoding="utf-8")
    status, steps = _status_text(w["conn"])
    rag_records = w["rag"]._news.get(include=["documents", "metadatas"])
    outputs = {
        "technical log": w["tech"],
        "root debug capture": "\n".join(w["capture"].lines),
        "warnings": "\n".join(f"{x.category.__name__} {x.message}" for x in w["caught"]),
        "activity": activity,
        "tables": _dump_tables(w["conn"]),
        "status": status,
        "rag": repr(rag_records),
    }
    for where, text in outputs.items():
        assert Q not in text, where
        assert "RuntimeError" not in text and "unexpected" not in text, where
    for where, text in outputs.items():
        if where != "rag":
            assert P not in text, where
    for doc, meta in zip(rag_records["documents"], rag_records["metadatas"]):
        if P in repr(meta) or P in doc:
            # a web source's display URL, or a feed article that itself links to that page
            assert meta["source_name"].startswith("web-") or meta["source_name"] == "feed-cross", meta
    assert [s.code for s in steps if s.code == "news_source_failing"], "failing sources reach status"
    assert "example.invalid" not in status and "2001:db8" not in status


def test_every_request_went_out_with_the_package_user_agent(world):
    from agentic_fx.datafeed.user_agent import user_agent
    assert world["seen"]
    assert {r.headers["user-agent"] for r in world["seen"]} == {user_agent()}


def test_failure_reasons_are_exact_fixed_words(world):
    details = activity_details(world["tmp"], "news_source_failed")
    seen = {}
    for line in details:
        name, _, reason = line.partition(": ")
        seen.setdefault(name, set()).add(reason)
    assert seen == {
        "e404": {"HTTP 404"}, "e429": {"HTTP 429"}, "e503": {"HTTP 503"},
        "etimeout": {"タイムアウト"}, "econn": {"接続できません"}, "ev6": {"接続できません"},
        "ebad": {"取得できません"}, "eboom": {"取得できません"},
    }


def test_an_unexpected_error_in_one_source_does_not_stop_the_others(world):
    names = {m["source_name"] for m in world["rag"]._news.get(include=["metadatas"])["metadatas"]}
    assert {"feed-ok", "web-one", "web-two", "v6-ok"} <= names


def test_two_web_sources_differing_only_in_query_both_stay(world):
    data = world["rag"]._news.get(include=["metadatas"])
    web = {i: m for i, m in zip(data["ids"], data["metadatas"]) if i.startswith("web:")}
    assert set(web) == {"web:web-one", "web:web-two"}
    assert {m["url"] for m in web.values()} == {f"{BASE}/web-same"}
    assert all(Q not in i for i in data["ids"])


def test_feed_and_web_sharing_a_link_both_survive_and_web_prefixed_links_are_skipped(world):
    data = world["rag"]._news.get(include=["metadatas"])
    by_id = dict(zip(data["ids"], data["metadatas"]))
    assert by_id[f"{BASE}/web-same"]["source_name"] == "feed-cross"
    assert by_id["web:web-one"]["source_name"] == "web-one"
    assert not [i for i in data["ids"] if i.startswith("javascript")]
    assert sorted(i for i in data["ids"] if not i.startswith("web:")) == sorted([
        "https://example.invalid/story-1", "https://example.invalid/story-2",
        f"{BASE}/web-same", "https://example.invalid/v6-story"])


def test_feed_ids_keep_the_link_and_web_titles_fall_back_to_the_source_name(world):
    data = world["rag"]._news.get(include=["metadatas"])
    by_id = dict(zip(data["ids"], data["metadatas"]))
    assert by_id["web:web-one"]["title"] == "web-one"
    assert by_id["https://example.invalid/story-1"]["url"] == "https://example.invalid/story-1"


def test_timezone_warning_is_in_the_technical_log_by_source_name(world):
    lines = [l for l in world["tech"].splitlines() if "unrecognized timezone" in l]
    assert lines and all("feed-ok" in l for l in lines)


def test_status_shows_failing_sources_by_name_only(world):
    status, steps = _status_text(world["conn"])
    codes = [s.code for s in steps]
    assert codes[-6:] == ["news_source_failing"] * 5 + ["news_source_failing_more"]
    rows = [(s.code, dict(s.params)) for s in steps if s.code.startswith("news_source")]
    assert rows == [("news_source_failing", {"name": n}) for n in ("e404", "e429", "e503", "ebad", "eboom")] \
        + [("news_source_failing_more", {"count": 3})]
