"""Shared fixtures for the ``afx news`` command tests."""
from __future__ import annotations

import io
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from agentic_fx import entry
from agentic_fx.datafeed import fetchers
from agentic_fx.store import news_sources
from agentic_fx.store.db import connect, init_db
from tests.status_support import write_settings

NOW = datetime(2026, 10, 25, 9, 30, tzinfo=timezone.utc)
CANARY_Q = "qcanary7f3a91"
CANARY_P = "pcanary5b2d84"
SECRET_URL = f"https://example.invalid/{CANARY_P}/feed?k={CANARY_Q}"
IPV6_URL = f"https://[2001:db8::1]/feed?k={CANARY_Q}"
OK_URL = "https://example.invalid/rss.xml"

RSS_TWO = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>a</title><link>https://example.invalid/a</link><description>body a</description></item>
<item><title>b</title><link>https://example.invalid/b</link></item>
</channel></rss>"""
RSS_EMPTY = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>"""


class FakeStdin(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool = False) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def make_root(tmp_path: Path, *, sources: tuple[tuple[str, str, str, bool], ...] = (),
              settings: str = "ok") -> Path:
    """A root with an initialised database (name, fetcher, url, enabled) and settings."""
    root = tmp_path / "root"
    root.mkdir()
    write_settings(root, settings)
    conn = connect(root / "data" / "agentic.db")
    try:
        init_db(conn)
        for name, fetcher, url, enabled in sources:
            news_sources.add(conn, name=name, fetcher=fetcher, url=url, added_by="user",
                             now=NOW, enabled=enabled)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        (root / "data" / f"agentic.db{suffix}").unlink(missing_ok=True)
    return root


def db_path(root: Path) -> Path:
    return root / "data" / "agentic.db"


def dump_tables(root: Path, *, skip: frozenset[str] = frozenset()) -> dict[str, list[tuple]]:
    """Every table's rows (logical dump); opens without creating sidecars when there are none."""
    path = db_path(root)
    has_sidecar = any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm"))
    uri = f"file:{path}?mode=ro" + ("" if has_sidecar else "&immutable=1")
    conn = sqlite3.connect(uri, uri=True)
    try:
        names = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]
                for name in names if name not in skip}
    finally:
        conn.close()


def run_news(capsys, monkeypatch, root: Path, *argv: str, stdin: FakeStdin | None = None):
    """Run ``afx news ...`` through ``entry.main``; returns (rc, stdout, stderr)."""
    monkeypatch.setattr(sys, "stdin", stdin if stdin is not None else FakeStdin())
    capsys.readouterr()
    rc = entry.main(["--root", str(root), "news", *argv])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


class FakeHttp:
    """Routes ``httpx.get`` through a MockTransport and records every request."""

    def __init__(self, monkeypatch, body: bytes = RSS_TWO, status: int = 200, headers: dict | None = None,
                 raises: BaseException | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.body, self.status, self.raises = body, status, raises
        self.headers = {"content-type": "application/rss+xml", **(headers or {})}

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.raises is not None:
                raise self.raises
            return httpx.Response(self.status, content=self.body, headers=self.headers, request=request)

        def get(url, **kwargs):
            allowed = {key: kwargs[key] for key in ("timeout", "follow_redirects", "headers") if key in kwargs}
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                return client.get(url, **allowed)

        monkeypatch.setattr(fetchers.httpx, "get", get)

    @property
    def count(self) -> int:
        return len(self.requests)
