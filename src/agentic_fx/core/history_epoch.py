"""History generation used by approval payload compare-and-swap checks."""
from __future__ import annotations

import sqlite3


def history_epoch(conn: sqlite3.Connection) -> str:
    """直近の time repair の名前 (無ければ `none`)。明示置換の revision があるときだけ、
    その最新 id を足した version 付きの token にする。revision が無い間は従来の文字列と同じ。
    revision の表が無い DB (旧 DB・観測用の接続) では revision なしとして扱う。"""
    row = conn.execute("SELECT name FROM data_migrations WHERE name LIKE 'time_repair_%' "
                       "ORDER BY applied_at DESC, name DESC LIMIT 1").fetchone()
    token = str(row[0]) if row else "none"
    try:
        revision = conn.execute("SELECT MAX(id) FROM history_revisions").fetchone()[0]
    except sqlite3.OperationalError:
        revision = None
    if revision is None:
        return token
    return f"v2;time_repair={token};revision={int(revision)}"
