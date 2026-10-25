from datetime import datetime, timezone

import pytest
import sqlite3

from agentic_fx.store import news_sources
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def test_add_and_enable(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    sid = news_sources.add(c, name="reuters-fx", fetcher="feed",
                           url="https://example.com/rss", added_by="user", now=NOW)
    assert news_sources.list_enabled(c) == []
    news_sources.set_enabled(c, sid, True)
    assert news_sources.list_enabled(c)[0]["name"] == "reuters-fx"
    with pytest.raises(sqlite3.IntegrityError):
        news_sources.add(c, name="dup", fetcher="feed",
                         url="https://example.com/rss", added_by="user", now=NOW)


def test_add_without_commit_can_be_rolled_back(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    c.commit()
    c.execute("BEGIN IMMEDIATE")
    news_sources.add(c, name="a", fetcher="feed", url="https://example.com/a",
                     added_by="user", now=NOW, commit=False)
    c.rollback()
    assert news_sources.list_all(c) == []


def _db(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def test_add_stores_the_normalized_url_and_rejects_invalid_ones(tmp_path):
    from agentic_fx.datafeed.news_url import NewsInputError
    c = _db(tmp_path)
    news_sources.add(c, name="a", fetcher="feed", url="HTTPS://Example.INVALID:443/Feed?K=1",
                     added_by="user", now=NOW)
    assert news_sources.list_all(c)[0]["url"] == "https://example.invalid/Feed?K=1"
    with pytest.raises(NewsInputError):
        news_sources.add(c, name="b", fetcher="feed", url="https://example.invalid/x#",
                         added_by="user", now=NOW)
    assert len(news_sources.list_all(c)) == 1


def test_find_duplicate_by_name_and_by_normalized_url(tmp_path):
    c = _db(tmp_path)
    news_sources.add(c, name="one", fetcher="feed", url="https://example.invalid/a?x=1",
                     added_by="user", now=NOW, enabled=False)
    news_sources.add(c, name="two", fetcher="web", url="https://example.invalid/b",
                     added_by="user", now=NOW, enabled=True)
    assert news_sources.find_duplicate(c, "one", "https://other.invalid/") == ("name", "one")
    assert news_sources.find_duplicate(c, "new", "HTTPS://EXAMPLE.invalid:443/a?x=1") == ("url", "one")
    assert news_sources.find_duplicate(c, "new", "https://example.invalid/b") == ("url", "two")
    assert news_sources.find_duplicate(c, "new", "https://example.invalid/a?x=2") is None
    assert news_sources.find_duplicate(c, "new", "https://example.invalid/a") is None
    assert news_sources.find_duplicate(c, "new", "https://example.invalid") is None
    # name wins when both collide
    assert news_sources.find_duplicate(c, "two", "https://example.invalid/a?x=1") == ("name", "two")


def test_find_duplicate_normalizes_legacy_rows_and_reports_no_url(tmp_path):
    c = _db(tmp_path)
    c.execute("INSERT INTO news_sources (name,fetcher,url,enabled,added_by,created_at) VALUES (?,?,?,?,?,?)",
              ("legacy", "feed", "HTTP://Legacy.INVALID:80/feed", 1, "user", NOW.isoformat()))
    c.execute("INSERT INTO news_sources (name,fetcher,url,enabled,added_by,created_at) VALUES (?,?,?,?,?,?)",
              ("odd", "feed", "not a url", 1, "user", NOW.isoformat()))
    c.commit()
    assert news_sources.find_duplicate(c, "n", "http://legacy.invalid/feed") == ("url", "legacy")
    assert news_sources.find_duplicate(c, "n", "https://example.invalid/") is None


def test_unique_constraint_is_still_the_final_judge(tmp_path):
    c = _db(tmp_path)
    news_sources.add(c, name="a", fetcher="feed", url="https://example.invalid/a", added_by="user", now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        news_sources.add(c, name="b", fetcher="feed", url="HTTPS://EXAMPLE.INVALID:443/a",
                         added_by="user", now=NOW)


def test_set_enabled_commit_flag(tmp_path):
    path = tmp_path / "t.db"
    a = connect(path)
    init_db(a)
    b = connect(path)
    sid = news_sources.add(a, name="a", fetcher="feed", url="https://example.invalid/a",
                           added_by="user", now=NOW)
    news_sources.set_enabled(a, sid, True, commit=False)
    assert news_sources.list_enabled(b) == []
    a.commit()
    assert len(news_sources.list_enabled(b)) == 1
    news_sources.set_enabled(a, sid, False)
    assert news_sources.list_enabled(b) == []


def test_importing_the_store_does_not_load_the_fetch_libraries():
    import subprocess
    import sys
    code = ("import sys, agentic_fx.store.news_sources, agentic_fx.setup.pending_facts;"
            "bad=[m for m in ('trafilatura','feedparser') if m in sys.modules];"
            "sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-I", "-c", code]).returncode == 0


# --- duplicate_of: the single decision shared by add and the init wizard ---------------------

def test_duplicate_of_reports_name_then_url_then_none():
    rows = [("a", "https://example.invalid/x"), ("b", "https://example.invalid/y")]
    assert news_sources.duplicate_of(rows, "a", "https://example.invalid/z") == ("name", "a")
    assert news_sources.duplicate_of(rows, "fresh", "https://example.invalid/y") == ("url", "b")
    assert news_sources.duplicate_of(rows, "fresh", "https://example.invalid/z") is None
    assert news_sources.duplicate_of(rows, "b", "https://example.invalid/x") == ("name", "b")


def test_duplicate_of_normalizes_both_sides_and_tolerates_unnormalizable_rows():
    rows = [("a", "HTTPS://Example.invalid:443/x"), ("broken", "not a url")]
    assert news_sources.duplicate_of(rows, "fresh", "https://example.invalid/x") == ("url", "a")
    assert news_sources.duplicate_of(rows, "fresh", "https://example.invalid:443/x") == ("url", "a")
    assert news_sources.duplicate_of(rows, "fresh", "https://example.invalid/other") is None
    assert news_sources.duplicate_of(iter(rows), "broken", "https://example.invalid/other") == ("name", "broken")


def test_find_duplicate_agrees_with_duplicate_of(tmp_path):
    c = _db(tmp_path)
    news_sources.add(c, name="a", fetcher="feed", url="https://example.invalid/x", added_by="user", now=NOW)
    for name, url in (("a", "https://example.invalid/q"), ("n", "HTTPS://EXAMPLE.invalid:443/x"),
                      ("n", "https://example.invalid/q")):
        rows = [(r["name"], r["url"]) for r in news_sources.list_all(c)]
        assert news_sources.find_duplicate(c, name, url) == news_sources.duplicate_of(rows, name, url)
