import pytest
from datetime import datetime, timedelta, timezone
from agentic_fx.store import alert_state
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def test_alert_state_roundtrip_single_known_key(tmp_path):
    c = connect(tmp_path / "a.db"); init_db(c)
    assert alert_state.get(c, alert_state.GATE_REJECT_STREAK_KEY) is None
    alert_state.set(c, alert_state.GATE_REJECT_STREAK_KEY, "7", now=NOW)
    assert alert_state.get(c, alert_state.GATE_REJECT_STREAK_KEY) == "7"


def test_alert_state_rejects_unknown_key(tmp_path):
    c = connect(tmp_path / "a.db"); init_db(c)
    with pytest.raises(ValueError, match="unknown alert_state key"):
        alert_state.set(c, "anything", "1", now=NOW)
    # get 側も同じ防御を持つ (レビュー 1 周目 ローカル LLM): set だけを
    # 検査していると `get` のガード削除変異が生き残り、未知キーが黙って
    # None を返す = 呼び出し側が「まだ通知していない」と誤認する。
    with pytest.raises(ValueError, match="unknown alert_state key"):
        alert_state.get(c, "anything")


def test_alert_state_set_overwrites_an_existing_key(tmp_path):
    """`set` の upsert (`ON CONFLICT(key) DO UPDATE`) の pin
    (レビュー 1 周目 qwen3.8-27b)。`test_alert_state_roundtrip_single_known_key`
    は同じキーへ **1 回しか** `set` しないため、`ON CONFLICT` 節を落として
    素の `INSERT` にする変異が生き残る。落とすと 2 回目の `set` が
    `IntegrityError` になり、`_notify_gate_reject_streak` の `except Exception`
    に握り潰される = **通知だけ飛んでラッチが更新されない**。以後その区間で
    毎周期通知が再送される (契約「1 区間につき 1 通」の破綻)。"""
    c = connect(tmp_path / "a.db"); init_db(c)
    alert_state.set(c, alert_state.GATE_REJECT_STREAK_KEY, "1", now=NOW)
    alert_state.set(c, alert_state.GATE_REJECT_STREAK_KEY, "2",
                    now=NOW + timedelta(seconds=1))
    assert alert_state.get(c, alert_state.GATE_REJECT_STREAK_KEY) == "2"
    rows = c.execute("SELECT key,value,updated_at FROM alert_state").fetchall()
    assert len(rows) == 1
    assert rows[0]["updated_at"] == (NOW + timedelta(seconds=1)).isoformat()
