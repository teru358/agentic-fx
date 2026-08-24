"""tests/plugin/test_strategy_gate.py 共有 fixture (プラン10 Task10-7)。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def conn_with_approved_strategy(conn):
    """10.7 節の baseline 照合 (`approval_requests.status='approved'` の
    最新行での近似、統合裁定 R-i12) が読む承認済み plugin 行を 1 件作る。
    `name="myst"` で作成する (loop-side の fixture 差異)。"""
    approvals_store.create(
        conn, kind="plugin",
        payload={"name": "myst", "kind": "strategy",
                 "content_hash": "baseline-hash"},
        now=NOW, commit=False)
    conn.execute(
        "UPDATE approval_requests SET status='approved' WHERE id="
        "(SELECT id FROM approval_requests ORDER BY id DESC LIMIT 1)")
    conn.commit()
    return conn


@pytest.fixture
def conn_with_pending_strategy(conn):
    """D-6 是正 (プラン申し送り B.6・10.7 M5 の pin 用)。
    `conn_with_approved_strategy` と同じ `name="myst"` の承認行を作るが
    `status` は `create()` の既定 (`pending`) のまま — `approved` へ更新
    しない。`AND status='approved'` を落とす退行 (10.7 M5) を殺すには
    「承認済みは 0 件だが承認 *申請* は存在する」状態が要る (行が 0 件だと
    WHERE 句を丸ごと消しても結果は変わらず退行を検出できない)。"""
    approvals_store.create(
        conn, kind="plugin",
        payload={"name": "myst", "kind": "strategy",
                 "content_hash": "baseline-hash"},
        now=NOW, commit=True)
    return conn
