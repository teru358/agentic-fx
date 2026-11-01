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


def test_dynamic_keys_use_known_prefixes_only(tmp_path):
    from datetime import datetime, timezone
    from agentic_fx.store.db import connect, init_db
    conn = connect(tmp_path / "a.db")
    init_db(conn)
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    key = alert_state.limit_suppress_key(7)
    assert key == "limit_suppress.7"
    alert_state.set(conn, key, "[]", now=now)
    alert_state.set(conn, alert_state.limit_suppress_key(8), "[]", now=now)
    assert alert_state.get(conn, key) == "[]"
    assert alert_state.keys_with_prefix(conn, alert_state.LIMIT_SUPPRESS_PREFIX) == [
        "limit_suppress.7", "limit_suppress.8"]
    alert_state.delete(conn, key)
    assert alert_state.get(conn, key) is None
    for bad in ("limit_suppress.", "limit_suppressX", "other.1"):
        with pytest.raises(ValueError):
            alert_state.get(conn, bad)
        with pytest.raises(ValueError):
            alert_state.set(conn, bad, "x", now=now)
        with pytest.raises(ValueError):
            alert_state.delete(conn, bad)
    with pytest.raises(ValueError):
        alert_state.keys_with_prefix(conn, "gate_reject.")
    conn.close()


def test_prefix_listing_does_not_treat_underscore_as_a_wildcard(tmp_path):
    from datetime import datetime, timezone
    from agentic_fx.store.db import connect, init_db
    conn = connect(tmp_path / "a.db")
    init_db(conn)
    conn.execute("INSERT INTO alert_state (key,value,updated_at) VALUES "
                 "('limitXsuppress.9','x','t')")
    conn.commit()
    assert alert_state.keys_with_prefix(conn, alert_state.LIMIT_SUPPRESS_PREFIX) == []
    conn.close()
