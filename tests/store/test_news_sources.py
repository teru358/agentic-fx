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


def test_seed_default_sources_commit_flag_defaults_to_committing(tmp_path):
    from agentic_fx.datafeed.news_collector import seed_default_sources

    c = connect(tmp_path / "t.db")
    init_db(c)
    c.commit()
    c.execute("BEGIN IMMEDIATE")
    added = seed_default_sources(c, NOW, commit=False)
    assert added > 0 and c.in_transaction
    c.rollback()
    assert news_sources.list_all(c) == []
    assert seed_default_sources(c, NOW) == added
    assert not c.in_transaction
    assert len(news_sources.list_all(c)) == added
