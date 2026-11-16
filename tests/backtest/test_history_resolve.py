"""衝突記録の人による解決 (keep / replace): CAS・epoch・revision・backup・原子性 (実 sqlite)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.backtest import history_resolve as hr
from agentic_fx.core.history_epoch import history_epoch
from agentic_fx.store import history_conflicts as hc
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.history_lease import history_mutation_lease

NOW = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
BAR = "2026-11-03T13:45:00+00:00"
OLD = (148.0, 148.2, 147.9, 148.1, 10.0, None)
NEW = (148.0, 148.3, 147.9, 148.25, 12.0, 0.01)


def _db(tmp_path):
    path = tmp_path / "t.db"
    conn = connect(path)
    init_db(conn)
    conn.execute("INSERT INTO ohlcv_history (symbol, interval, bar_time, open, high, low, close, "
                 "volume, source, spread) VALUES ('USDJPY','15m',?,?,?,?,?,?,'mt5',?)",
                 (BAR, *OLD))
    conn.commit()
    return conn, path


def _sync_run(conn, run_id="r1", *, source="mt5", interval="15m"):
    conn.execute(
        "INSERT OR IGNORE INTO history_sync_runs (id, owner, trigger, status, source, interval, "
        "calendar_version, lookback_days, history_epoch, t_utc, b_utc, e_utc, started_at) "
        "VALUES (?, 'offline', 'manual', 'ok', ?, ?, 'v', 2, 'none', 't', 'b', 'e', 's')",
        (run_id, source, interval))
    conn.commit()


def _conflict(conn, *, incoming=NEW, origin="sync", bar=BAR, state=None, verified="r1"):
    if verified:
        _sync_run(conn, verified)
    hc.upsert(conn, source="mt5", interval="15m", symbol="USDJPY", bar_time=bar,
              history_epoch=history_epoch(conn), existing=OLD, incoming=incoming,
              origin=origin, seen_at="2026-11-13T00:00:00+00:00", run_id=verified,
              verified_run_id=verified)
    conn.commit()
    cid = conn.execute("SELECT MAX(id) FROM history_conflicts").fetchone()[0]
    if state:
        conn.execute("UPDATE history_conflicts SET state=? WHERE id=?", (state, cid))
        conn.commit()
    return cid


def _row(conn, cid):
    return dict(conn.execute("SELECT * FROM history_conflicts WHERE id=?", (cid,)).fetchone())


def _bar(conn):
    return tuple(conn.execute("SELECT open, high, low, close, volume, spread FROM ohlcv_history "
                              "WHERE bar_time=?", (BAR,)).fetchone())


def _resolve(conn, path, cid, action, reason="人が確認した", **kw):
    return hr.resolve(conn, path, conflict_id=cid, action=action, reason=reason, now=NOW, **kw)


# ---- keep -----------------------------------------------------------------

def test_keep_existing_records_the_decision_and_changes_neither_history_nor_epoch(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    epoch = history_epoch(conn)
    done = _resolve(conn, path, cid, "keep-existing")
    assert (done.state, done.changed) == ("kept", True)
    row = _row(conn, cid)
    assert (row["state"], row["resolution_action"], row["resolved_by"], row["reason_ref"]) == (
        "kept", "keep_existing", "human:cli", "人が確認した")
    assert row["resolved_at"] == NOW.isoformat()
    assert _bar(conn) == OLD and history_epoch(conn) == epoch
    assert conn.execute("SELECT COUNT(*) FROM history_revisions").fetchone()[0] == 0


def test_keep_existing_is_idempotent_and_never_reopens(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    _resolve(conn, path, cid, "keep-existing")
    again = _resolve(conn, path, cid, "keep-existing", reason="別の理由")
    assert again.changed is False and _row(conn, cid)["reason_ref"] == "人が確認した"


@pytest.mark.parametrize("state", ["replaced", "superseded"])
def test_keep_on_a_closed_conflict_is_refused(tmp_path, state):
    conn, path = _db(tmp_path)
    cid = _conflict(conn, state=state)
    with pytest.raises(hr.ResolveError) as err:
        _resolve(conn, path, cid, "keep-existing")
    assert (err.value.code, err.value.rc) == ("conflict_closed", 1)
    assert _row(conn, cid)["state"] == state


# ---- replace --------------------------------------------------------------

def test_replace_writes_incoming_audits_and_changes_the_epoch_atomically(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    other = _conflict(conn, incoming=(148.0, 148.4, 147.9, 148.3, 11.0, None))   # 同じ足の別の異値
    before = history_epoch(conn)
    done = _resolve(conn, path, cid, "replace-with-incoming")
    assert (done.state, done.changed, done.epoch_before) == ("replaced", True, before)
    assert _bar(conn) == NEW
    row = _row(conn, cid)
    assert (row["state"], row["resolution_action"], row["revision_id"]) == (
        "replaced", "replace_with_incoming", str(done.revision_id))
    sup = _row(conn, other)
    assert (sup["state"], sup["resolution_action"], sup["revision_id"]) == (
        "superseded", "supersede", str(done.revision_id))
    rev = conn.execute("SELECT * FROM history_revisions").fetchone()
    assert (rev["id"], rev["conflict_id"], rev["history_epoch"], rev["actor"]) == (
        done.revision_id, cid, before, "human:cli")
    assert history_epoch(conn) == done.epoch_after != before
    assert done.epoch_after == f"v2;time_repair=none;revision={done.revision_id}"


def test_replace_takes_a_verified_backup_before_the_change(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    done = _resolve(conn, path, cid, "replace-with-incoming")
    backup = Path(done.backup)
    assert backup.exists() and backup.name.startswith(f"{path.name}.bak-history-replace-")
    copy = sqlite3.connect(backup)
    assert copy.execute("SELECT close FROM ohlcv_history WHERE bar_time=?", (BAR,)
                        ).fetchone()[0] == OLD[3]                # 変更前の値
    assert copy.execute("SELECT COUNT(*) FROM history_revisions").fetchone()[0] == 0
    copy.close()


def test_a_replaced_conflict_cannot_be_replaced_again(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    _resolve(conn, path, cid, "replace-with-incoming")
    with pytest.raises(hr.ResolveError) as err:
        _resolve(conn, path, cid, "replace-with-incoming")
    assert err.value.code == "conflict_closed"
    assert conn.execute("SELECT COUNT(*) FROM history_revisions").fetchone()[0] == 1


def test_a_kept_conflict_can_still_be_replaced_later(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    _resolve(conn, path, cid, "keep-existing")
    done = _resolve(conn, path, cid, "replace-with-incoming")
    assert done.state == "replaced" and _bar(conn) == NEW


@pytest.mark.parametrize("origin", ["live_compare"])
def test_a_live_compare_record_is_never_replaceable_by_origin_or_last_origin(tmp_path, origin):
    conn, path = _db(tmp_path)
    cid = _conflict(conn, origin=origin)
    with pytest.raises(hr.ResolveError) as err:
        _resolve(conn, path, cid, "replace-with-incoming")
    assert err.value.code == "replace_refused_live_compare"
    assert _bar(conn) == OLD and conn.execute("SELECT COUNT(*) FROM history_revisions").fetchone()[0] == 0
    cid2 = _conflict(conn, incoming=(1.0, 2.0, 0.5, 1.5, 1.0, None))
    conn.execute("UPDATE history_conflicts SET last_origin='live_compare' WHERE id=?", (cid2,))
    conn.commit()
    with pytest.raises(hr.ResolveError) as err:
        _resolve(conn, path, cid2, "replace-with-incoming")
    assert err.value.code == "replace_refused_live_compare"


def test_cas_refuses_when_the_existing_value_changed_after_the_record(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    conn.execute("UPDATE ohlcv_history SET close=149.0 WHERE bar_time=?", (BAR,))
    conn.commit()
    with pytest.raises(hr.ResolveError) as err:
        _resolve(conn, path, cid, "replace-with-incoming")
    assert err.value.code == "existing_changed"
    assert _bar(conn)[3] == 149.0 and _row(conn, cid)["state"] == "open"
    assert conn.execute("SELECT COUNT(*) FROM history_revisions").fetchone()[0] == 0


def test_cas_refuses_when_the_epoch_moved_after_the_record(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    conn.execute("INSERT INTO data_migrations (name, applied_at) VALUES ('time_repair_x', ?)",
                 (NOW.isoformat(),))
    conn.commit()
    with pytest.raises(hr.ResolveError) as err:
        _resolve(conn, path, cid, "replace-with-incoming")
    assert err.value.code == "epoch_changed" and _bar(conn) == OLD


def test_a_missing_history_row_is_refused(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    conn.execute("DELETE FROM ohlcv_history")
    conn.commit()
    with pytest.raises(hr.ResolveError) as err:
        _resolve(conn, path, cid, "replace-with-incoming")
    assert err.value.code == "history_row_missing"


@pytest.mark.parametrize("point", ["after_revision", "before_commit"])
def test_a_failure_before_commit_rolls_back_history_revision_and_audit_together(tmp_path, point):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    epoch = history_epoch(conn)

    def boom(name):
        if name == point:
            raise RuntimeError("injected")
    with pytest.raises(RuntimeError):
        _resolve(conn, path, cid, "replace-with-incoming", hook=boom)
    assert not conn.in_transaction
    assert _bar(conn) == OLD and history_epoch(conn) == epoch
    assert conn.execute("SELECT COUNT(*) FROM history_revisions").fetchone()[0] == 0
    row = _row(conn, cid)
    assert (row["state"], row["resolved_at"], row["revision_id"]) == ("open", None, None)


# ---- 入力・競合 -----------------------------------------------------------

@pytest.mark.parametrize("reason", [None, "", "   ", "x" * (hr.REASON_MAX + 1), "bad\x00reason"])
def test_a_missing_or_invalid_reason_is_rc_2_before_anything_is_touched(tmp_path, reason):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    with pytest.raises(hr.ResolveError) as err:
        hr.resolve(conn, path, conflict_id=cid, action="keep-existing", reason=reason)
    assert err.value.rc == 2 and _row(conn, cid)["state"] == "open"


def test_unknown_action_and_unknown_id_are_rc_2(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    for action, ident in (("delete", cid), ("keep-existing", cid + 99)):
        with pytest.raises(hr.ResolveError) as err:
            _resolve(conn, path, ident, action)
        assert err.value.rc == 2


@pytest.mark.parametrize("action", ["keep-existing", "replace-with-incoming"])
def test_a_busy_history_mutation_lease_is_database_busy_with_nothing_written(tmp_path, action):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    other = connect(path)
    with history_mutation_lease(other):
        with pytest.raises(hr.ResolveError) as err:
            _resolve(conn, path, cid, action)
    assert (err.value.code, err.value.rc) == ("database_busy", 1)
    assert _row(conn, cid)["state"] == "open" and _bar(conn) == OLD


def test_the_reason_text_is_kept_only_in_the_human_audit_columns(tmp_path, caplog):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    secret = "reason-canary-" + "x".join(["a", "b"])
    with caplog.at_level("DEBUG"):
        done = _resolve(conn, path, cid, "replace-with-incoming", reason=secret)
    assert secret not in caplog.text and secret not in repr(done)
    assert conn.execute("SELECT reason_ref FROM history_revisions").fetchone()[0] == secret


# ---- 置換の根拠 (検証済みの MT5 取得への結び付け) ----------------------------------

def _refused(conn, path, cid):
    before = (_bar(conn), conn.execute("SELECT COUNT(*) FROM history_revisions").fetchone()[0],
              history_epoch(conn))
    with pytest.raises(hr.ResolveError) as err:
        _resolve(conn, path, cid, "replace-with-incoming")
    assert err.value.code == "replace_refused_unverified" and "afx history sync" in err.value.hint
    after = (_bar(conn), conn.execute("SELECT COUNT(*) FROM history_revisions").fetchone()[0],
             history_epoch(conn))
    assert before == after and _row(conn, cid)["state"] in ("open", "kept")
    assert not list(path.parent.glob("*.bak-history-replace-*"))      # backup も作らない


def test_a_record_without_a_bridge_proof_is_refused_for_replace_but_keep_is_allowed(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn, origin="import", verified=None)
    _refused(conn, path, cid)
    assert _resolve(conn, path, cid, "keep-existing").state == "kept"
    _refused(conn, path, cid)                                      # keep のあとも根拠は増えない


def test_a_proof_pointing_to_a_missing_or_foreign_run_is_refused(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    conn.execute("DELETE FROM history_sync_runs")
    conn.commit()
    _refused(conn, path, cid)
    _sync_run(conn, "r1", interval="1m")                            # 足幅が違う run
    _refused(conn, path, cid)
    conn.execute("DELETE FROM history_sync_runs")
    _sync_run(conn, "r1", source="dukascopy")                      # source が違う run
    _refused(conn, path, cid)


def test_a_non_sync_last_observation_is_refused_even_with_a_proof_column_value(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)
    conn.execute("UPDATE history_conflicts SET last_origin='import' WHERE id=?", (cid,))
    conn.commit()
    _refused(conn, path, cid)


def test_the_proof_is_rechecked_inside_the_replace_transaction(tmp_path):
    conn, path = _db(tmp_path)
    cid = _conflict(conn)

    def drop_proof(point):
        if point == "after_backup":
            conn.execute("UPDATE history_conflicts SET bridge_verified_run_id=NULL WHERE id=?",
                         (cid,))
            conn.commit()
    with pytest.raises(hr.ResolveError) as err:
        _resolve(conn, path, cid, "replace-with-incoming", hook=drop_proof)
    assert err.value.code == "replace_refused_unverified"
    assert _bar(conn) == OLD
    assert conn.execute("SELECT COUNT(*) FROM history_revisions").fetchone()[0] == 0


def test_an_unverified_reobservation_returns_the_proof_to_null(tmp_path):
    conn, _path = _db(tmp_path)
    cid = _conflict(conn)
    assert _row(conn, cid)["bridge_verified_run_id"] == "r1"
    hc.upsert(conn, source="mt5", interval="15m", symbol="USDJPY", bar_time=BAR,
              history_epoch=history_epoch(conn), existing=OLD, incoming=NEW, origin="import",
              seen_at="2026-11-14T00:00:00+00:00")
    conn.commit()
    row = _row(conn, cid)
    assert row["bridge_verified_run_id"] is None and row["seen_count"] == 2
