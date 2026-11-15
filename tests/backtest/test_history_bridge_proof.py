"""衝突の置換の根拠: 検証済みの MT5 取得 (sync) で最後に観測された記録だけが置換できる。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agentic_fx.backtest import history_resolve as hr
from agentic_fx.store import history_sync as store
from tests.backtest.history_sync_support import SYMBOL, UTC, FakeBridge
from tests.backtest.test_history_sync import E, T, _run, _seeded
from tests.backtest.test_history_sync_resume import _partial_kw


def _dt(*a):
    return datetime(*a, tzinfo=UTC)


IN_APPEND = _dt(2026, 11, 10, 9)            # 追記範囲 (後ろ窓) 内
IN_TAIL = _dt(2026, 11, 9, 10, 0)           # 後ろ窓の始端 11-09 11:30 の前の検査尾の内


def _bridge(*times, delta=0.3):
    return FakeBridge(change={(SYMBOL, t): delta for t in times})


def _records(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM history_conflicts ORDER BY id")]


def _replace(conn, tmp_path, cid):
    return hr.resolve(conn, tmp_path / "t.db", conflict_id=cid, action="replace-with-incoming",
                      reason="人が確認した", now=T)


def test_an_old_record_without_a_run_gains_a_proof_from_a_sync_reobservation_and_is_replaceable(
        tmp_path):
    conn, settings = _seeded(tmp_path)
    first = _run(conn, settings, _bridge(IN_APPEND))
    assert first.conflicted == 1
    # sync が付ける前の形 (旧い手動 import の記録) に戻す
    conn.execute("UPDATE history_conflicts SET bridge_verified_run_id=NULL, last_origin='import', "
                 "origin='import', last_run_id=NULL, first_run_id=NULL")
    conn.commit()
    cid = _records(conn)[0]["id"]
    with pytest.raises(hr.ResolveError) as err:
        _replace(conn, tmp_path, cid)
    assert err.value.code == "replace_refused_unverified"
    second = _run(conn, settings, _bridge(IN_APPEND))                # 同 epoch・同 digest の再観測
    assert second.conflicted == 1 and second.ops[0].conflicted_known == 1
    row = _records(conn)[0]
    assert row["id"] == cid and row["bridge_verified_run_id"] == second.run_id
    assert row["last_origin"] == "sync" and row["origin"] == "import"
    done = _replace(conn, tmp_path, cid)
    assert done.state == "replaced" and done.revision_id == 1


def test_a_conflict_inside_the_inspection_tail_gets_a_proof(tmp_path):
    conn, settings = _seeded(tmp_path)
    result = _run(conn, settings, _bridge(IN_TAIL))
    row = _records(conn)[0]
    assert row["bar_time"] == IN_TAIL.isoformat()
    assert row["bridge_verified_run_id"] == result.run_id


def test_a_partial_operation_proves_only_its_confirmed_batches(tmp_path):
    conn, settings = _seeded(tmp_path)
    early, late = _dt(2026, 11, 9, 12), _dt(2026, 11, 11, 7)
    result = _run(conn, settings, _bridge(early, late), run_kw=_partial_kw(9))
    assert result.ops[0].status == "partial"
    rows = _records(conn)
    assert [r["bar_time"] for r in rows] == [early.isoformat()]      # 確定した prefix の分だけ
    assert rows[0]["bridge_verified_run_id"] == result.run_id


def test_a_rolled_back_batch_leaves_neither_a_record_nor_a_proof(tmp_path):
    conn, settings = _seeded(tmp_path)
    result = _run(conn, settings, _bridge(_dt(2026, 11, 9, 12)), run_kw=_partial_kw(3))
    assert result.ops[0].status == "failed" and result.ops[0].inserted == 0
    assert _records(conn) == []


def test_an_open_circuit_does_not_touch_an_existing_record_or_its_proof(tmp_path):
    conn, settings = _seeded(tmp_path)
    _run(conn, settings, _bridge(IN_APPEND))
    conn.execute("UPDATE history_conflicts SET bridge_verified_run_id=NULL, last_origin='import'")
    conn.commit()
    before = _records(conn)
    result = _run(conn, settings, FakeBridge(shift=5.0))
    assert result.ops[0].status == "conflict_circuit_open"
    assert _records(conn) == before


def test_an_insufficient_inspection_does_not_prove_an_existing_record(tmp_path):
    conn, settings = _seeded(tmp_path)
    _run(conn, settings, _bridge(IN_APPEND))
    conn.execute("UPDATE history_conflicts SET bridge_verified_run_id=NULL, last_origin='import'")
    conn.commit()
    before = _records(conn)
    # 応答が既存の足をほとんど返さず、比較できる重複が K 本に満たない (衝突の足だけは返る)
    bridge = FakeBridge(change={(SYMBOL, IN_APPEND): 0.3},
                        drop=lambda _s, t: t != IN_APPEND and t < E - timedelta(minutes=15))
    result = _run(conn, settings, bridge)
    assert result.ops[0].status == "deferred" and result.ops[0].reason == "inspection_insufficient"
    assert _records(conn) == before


def test_a_different_incoming_value_is_a_separate_record_with_its_own_proof(tmp_path):
    conn, settings = _seeded(tmp_path)
    first = _run(conn, settings, _bridge(IN_APPEND, delta=0.3))
    second = _run(conn, settings, _bridge(IN_APPEND, delta=0.4))
    rows = _records(conn)
    assert len(rows) == 2 and {r["incoming_digest"] for r in rows} == {
        rows[0]["incoming_digest"], rows[1]["incoming_digest"]} and len(
        {r["incoming_digest"] for r in rows}) == 2
    assert {r["bridge_verified_run_id"] for r in rows} == {first.run_id, second.run_id}


def test_a_manual_import_reobservation_removes_the_proof(tmp_path):
    from agentic_fx.backtest.mt5_import import import_mt5
    conn, settings = _seeded(tmp_path)
    _run(conn, settings, _bridge(IN_APPEND))
    assert _records(conn)[0]["bridge_verified_run_id"] is not None
    import_mt5(conn, SYMBOL, IN_APPEND - timedelta(hours=1), IN_APPEND + timedelta(hours=1),
               base_url="http://bridge.invalid", fetch=_bridge(IN_APPEND), interval="15m",
               clock=lambda: T)
    row = _records(conn)[0]
    assert row["bridge_verified_run_id"] is None and row["last_origin"] == "import"
    with pytest.raises(hr.ResolveError) as err:
        _replace(conn, tmp_path, row["id"])
    assert err.value.code == "replace_refused_unverified"


def test_the_proof_run_survives_retention_gc_and_the_replace_still_works(tmp_path):
    conn, settings = _seeded(tmp_path)
    result = _run(conn, settings, _bridge(IN_APPEND))
    other = _run(conn, settings)                                      # 衝突を持たない別の run
    _run(conn, settings)                                              # 系列状態の参照を先へ送る
    # 証明は最後の観測と同じ run を指すが、参照の元が他に無くても残ることを確かめる
    conn.execute("UPDATE history_conflicts SET first_run_id=NULL, last_run_id=NULL")
    conn.commit()
    late = T + timedelta(days=200)
    store.write_txn(conn, lambda c: store.collect_garbage(c, now=late), sleep=lambda _s: None)
    kept = conn.execute("SELECT COUNT(*) FROM history_sync_series WHERE run_id=?",
                        (result.run_id,)).fetchone()[0]
    gone = conn.execute("SELECT COUNT(*) FROM history_sync_series WHERE run_id=?",
                        (other.run_id,)).fetchone()[0]
    assert kept > 0 and gone == 0
    assert conn.execute("SELECT tombstone FROM history_sync_runs WHERE id=?",
                        (result.run_id,)).fetchone()[0] == 1
    assert _replace(conn, tmp_path, _records(conn)[0]["id"]).state == "replaced"
