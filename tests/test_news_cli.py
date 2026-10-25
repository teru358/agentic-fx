from __future__ import annotations

import sqlite3
from argparse import Namespace
from datetime import datetime, timezone

import pytest

from agentic_fx.store.db import connect_existing_rw
from agentic_fx import entry
from agentic_fx.news_cli import dispatch_news
from agentic_fx.store.db import connect, init_db
from agentic_fx.store import news_sources
from agentic_fx.datafeed import news_probe


def test_existing_rw_connection_never_creates_a_database(tmp_path):
    """Changing mode=rw to mode=rwc must make this test fail."""
    path = tmp_path / "data" / "agentic.db"

    with pytest.raises(FileNotFoundError):
        connect_existing_rw(path)

    assert not path.parent.exists()


def test_existing_rw_connection_configures_a_real_existing_database(tmp_path):
    path = tmp_path / "agentic.db"
    sqlite3.connect(path).close()

    conn = connect_existing_rw(path)
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_news_is_a_local_command_and_missing_db_does_not_start_service(tmp_path, monkeypatch, capsys):
    """Removing the early news dispatch must make this test fail."""
    monkeypatch.setattr(entry.service, "run_service", lambda *a, **k: pytest.fail("service started"))
    monkeypatch.chdir(tmp_path)

    assert entry.main(["news", "list"]) == 1
    assert capsys.readouterr().err == "ニュース源を読めません (DB がありません)。\n"


def test_list_folds_source_url_and_does_not_mutate_database(tmp_path, capsys):
    """Replacing fold_url with a raw URL must make this test fail."""
    path = tmp_path / "data" / "agentic.db"
    conn = connect(path)
    init_db(conn)
    news_sources.add(conn, name="daily-feed", fetcher="feed",
                     url="https://example.invalid/private/path?k=canary",
                     added_by="user", enabled=True, now=datetime.now(timezone.utc))
    conn.close()

    assert dispatch_news(Namespace(news_command="list"), tmp_path) == 0
    out = capsys.readouterr().out
    assert "https://example.invalid/…" in out
    assert "private" not in out and "canary" not in out
