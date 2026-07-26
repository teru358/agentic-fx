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
