# tests/loops/test_improve_loop_archive_index.py
"""T4 (§1 L3/L4 note/INDEX): `best_candidate` の順序、note/INDEX への
best 反映、`plugins/_archive/INDEX.md` の追記規約 (ブリーフ「変更点」1〜3、
「テスト」節)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.loops.improve_loop import _best_label, best_candidate
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.plugin.version_store import artifact_hash_bytes
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import candidate_archives


NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


def _row(id_, *, evaluable=True, trades=10, pf=1.5, max_drawdown=0.1,
        name="cand", artifact_hash="abcdef1234567890", archive_path=None):
    return {
        "id": id_, "name": name, "artifact_hash": artifact_hash,
        "archive_path": archive_path or f"plugins/_archive/1/{artifact_hash}",
        "metrics": {"evaluable": evaluable, "trades": trades, "pf": pf,
                    "max_drawdown": max_drawdown},
    }


# ---------------------------------------------------------------------------
# best_candidate 順序
# ---------------------------------------------------------------------------

def test_best_candidate_empty_rows_returns_none():
    assert best_candidate([]) is None


def test_best_candidate_prefers_evaluable_true():
    rows = [_row(1, evaluable=False, pf=99.0), _row(2, evaluable=True, pf=0.1)]
    assert best_candidate(rows)["id"] == 2


def test_best_candidate_prefers_more_trades_when_evaluable_ties():
    rows = [_row(1, trades=5), _row(2, trades=20)]
    assert best_candidate(rows)["id"] == 2


def test_best_candidate_prefers_higher_pf_when_trades_tie():
    rows = [_row(1, trades=10, pf=1.0), _row(2, trades=10, pf=2.0)]
    assert best_candidate(rows)["id"] == 2


def test_best_candidate_pf_none_sorts_last():
    rows = [_row(1, trades=10, pf=None), _row(2, trades=10, pf=0.01)]
    assert best_candidate(rows)["id"] == 2


def test_best_candidate_pf_non_numeric_sorts_last():
    rows = [_row(1, trades=10, pf="n/a"), _row(2, trades=10, pf=0.01)]
    assert best_candidate(rows)["id"] == 2


def test_best_candidate_prefers_lower_drawdown_when_pf_ties():
    rows = [_row(1, pf=1.0, max_drawdown=0.5), _row(2, pf=1.0, max_drawdown=0.1)]
    assert best_candidate(rows)["id"] == 2


def test_best_candidate_drawdown_none_sorts_last():
    rows = [_row(1, pf=1.0, max_drawdown=None), _row(2, pf=1.0, max_drawdown=0.2)]
    assert best_candidate(rows)["id"] == 2


def test_best_candidate_tie_breaks_by_ascending_id():
    rows = [_row(5), _row(2), _row(9)]
    assert best_candidate(rows)["id"] == 2


def test_best_candidate_treats_bool_metrics_as_non_numeric():
    """ローカル T4 1 周目 #L1 (2026-09-10): `bool` は `int` の subclass。
    `_is_numeric` の `not isinstance(value, bool)` を落とすと `True` が
    `pf=1.0` / `trades=1` として順位に参加してしまう。契約 (ブリーフ 1) の
    「None / 非数値は最下」に bool も含まれることを固定する。"""
    rows = [_row(1, trades=10, pf=True), _row(2, trades=10, pf=0.01)]
    assert best_candidate(rows)["id"] == 2

    rows = [_row(1, trades=True), _row(2, trades=1)]
    assert best_candidate(rows)["id"] == 2

    rows = [_row(3, pf=1.0, max_drawdown=False),
            _row(4, pf=1.0, max_drawdown=0.9)]
    assert best_candidate(rows)["id"] == 4

    label = _best_label(_row(1, pf=True, max_drawdown=False))
    assert "pf=-" in label and "dd=-" in label


def test_best_label_formats_pf_and_dd_to_three_decimals():
    row = _row(1, pf=1.23456, max_drawdown=0.04321,
               artifact_hash="0123456789abcdef", name="strat_x")
    label = _best_label(row)
    assert label == "strat_x@01234567 pf=1.235 dd=0.043"


def test_best_label_none_when_metric_missing():
    row = _row(1, pf=None, max_drawdown=None)
    label = _best_label(row)
    assert "pf=-" in label and "dd=-" in label


# ---------------------------------------------------------------------------
# note の best (fixtures は test_improve_loop_ledger_preserve.py と同じ流儀)
# ---------------------------------------------------------------------------


def _snapshot(path):
    path.mkdir(parents=True)
    blobs = (b"plugin = 1\n", b"name: x\n", b"def test_x(): pass\n")
    for name, data in zip(("plugin.py", "config.yaml", "test_plugin.py"), blobs):
        (path / name).write_bytes(data)
    return artifact_hash_bytes(*blobs)


def _ctx(tmp_path, mission_id, run_id, entries):
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    for entry in entries:
        ledger.record(**entry)
    ledger.freeze()
    staging = tmp_path / "plugins" / "_staging" / str(mission_id)
    staging.mkdir(parents=True, exist_ok=True)
    return ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging,
        source_snapshot_dir=staging / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})


def _backtest_entry(tmp, artifact_hash, *, metrics=None):
    return {
        "opaque_ref": f"bt:{tmp.name}", "kind": "run_backtest",
        "params": {"name": "x", "pair": "USDJPY"}, "trial_count": 1,
        "result_summary": {
            "scope": "in_sample", "plugin_ref": "plugins/x",
            "content_hash": "content", "kind": "strategy",
            "pair": "USDJPY", "timeframe": "1h", "source": "test",
            "base_interval": "1m", "period": (NOW, NOW),
            "metrics": metrics or {"pf": 1.5, "trades": 40,
                                   "max_drawdown": 0.08, "evaluable": True},
            "settings_hash": "settings", "core_commit": "core",
            "initial_balance": 10000.0, "now": NOW, "params": {},
            "artifact_hash": artifact_hash, "archive_tmp": str(tmp),
        },
    }


def test_finalize_failed_mission_note_ends_with_best_when_archive_exists(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, _ = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_failed_mission(
        conn, ctx=ctx,
        result=MissionResult(status="timeout", output=None, transcript=[]),
        now=NOW)

    note = conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE source='system' "
        "AND status='note' ORDER BY id DESC LIMIT 1").fetchone()
    assert note is not None
    assert f"best=x@{artifact_hash[:8]}" in note["last_result"]
    assert "archive=plugins/_archive/" in note["last_result"]


def test_finalize_failed_mission_note_has_no_best_when_no_archive(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, _ = mission_and_run_fixture
    ctx = _ctx(tmp_path, mission_id, run_id, [])

    loop_min._finalize_failed_mission(
        conn, ctx=ctx,
        result=MissionResult(status="timeout", output=None, transcript=[]),
        now=NOW)

    note = conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE source='system' "
        "AND status='note' ORDER BY id DESC LIMIT 1").fetchone()
    assert note is not None
    assert "best=" not in note["last_result"]


def test_finalize_failed_mission_note_survives_list_by_mission_exception(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """テスト節: `list_by_mission` 例外でも note と終端は続行 (activity)。"""
    mission_id, run_id, _ = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])
    monkeypatch.setattr(
        candidate_archives, "list_by_mission",
        lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))

    loop_min._finalize_failed_mission(
        conn, ctx=ctx,
        result=MissionResult(status="timeout", output=None, transcript=[]),
        now=NOW)

    note = conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE source='system' "
        "AND status='note' ORDER BY id DESC LIMIT 1").fetchone()
    assert note is not None
    assert "best=" not in note["last_result"]
    assert conn.execute(
        "SELECT status FROM missions WHERE id=?",
        (mission_id,)).fetchone()[0] == "failed"
    assert "best_candidate_lookup_failed" in loop_min._activity._path.read_text()


def test_finalize_output_invalid_writes_note_with_best(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, _ = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_output_invalid(
        conn, ctx=ctx, reason="schema_invalid:missing name", now=NOW)

    note = conn.execute(
        "SELECT idea, last_result FROM improvement_backlog "
        "WHERE source='system' AND status='note' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert note is not None
    assert "schema" in note["idea"]
    assert f"best=x@{artifact_hash[:8]}" in note["last_result"]


def test_finalize_output_invalid_note_has_no_best_when_no_archive(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, _ = mission_and_run_fixture
    ctx = _ctx(tmp_path, mission_id, run_id, [])

    loop_min._finalize_output_invalid(
        conn, ctx=ctx, reason="schema_invalid:missing name", now=NOW)

    note = conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE source='system' "
        "AND status='note' ORDER BY id DESC LIMIT 1").fetchone()
    assert note is not None
    assert "best=" not in note["last_result"]


# ---------------------------------------------------------------------------
# INDEX.md
# ---------------------------------------------------------------------------


def _index_path(loop):
    return loop._root / "plugins" / "_archive" / "INDEX.md"


def test_finalize_gate_failed_appends_index_row(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="gate_failed:test",
        now=NOW)

    text = _index_path(loop_min).read_text()
    assert f"| date | mission | status |" in text
    assert f"mission {mission_id}" in text
    assert "| gate_failed |" in text
    assert f"best=x@{artifact_hash[:8]}" in text
    # ローカル T4 1 周目 #L4 (2026-09-10): 日付セルは `now.isoformat()`
    # (ブリーフ「変更点」3 の `| <now ISO> |`)。`str(now)` に落とすと
    # 空白区切りになり ISO 形式でなくなる。
    date_cell = text.strip().splitlines()[-1].split("|")[1].strip()
    assert date_cell == NOW.isoformat()
    assert "T" in date_cell and " " not in date_cell


def test_index_append_is_fsynced(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """ローカル T4 1 周目 #L6 (2026-09-10): INDEX.md の追記は
    `flush` + `os.fsync` で永続化する (ブリーフ「変更点」3)。fsync を
    落としてもテストが緑のままだったので、INDEX の fd に対して
    実際に fsync が呼ばれることを固定する。"""
    import os as _os

    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    real_fsync = _os.fsync
    synced: list[str] = []

    def spy_fsync(fd):
        try:
            synced.append(_os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:  # pragma: no cover - /proc が無い環境
            synced.append("")
        return real_fsync(fd)

    monkeypatch.setattr(_os, "fsync", spy_fsync)

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="gate_failed:test",
        now=NOW)

    index_path = str(_index_path(loop_min))
    assert index_path in synced, synced


def test_finalize_loser_does_not_write_index_row_with_zero_candidates(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """行 0 件の mission は書かない。"""
    mission_id, run_id, _ = mission_and_run_fixture
    ctx = _ctx(tmp_path, mission_id, run_id, [])

    loop_min._finalize_loser(
        conn, ctx=ctx, output={"selected": {"idea": "dup"}}, now=NOW)

    assert not _index_path(loop_min).exists()


def test_finalize_success_does_not_write_index_row_for_approval(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """approval は書かない。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)

    assert not _index_path(loop_min).exists()


def test_index_header_written_only_once(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])
    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="gate_failed:1",
        now=NOW)

    mission_id2, run_id2, backlog_id2 = _second_mission(conn)
    tmp2 = tmp_path / "archive2"
    artifact_hash2 = _snapshot(tmp2)
    ctx2 = _ctx(tmp_path, mission_id2, run_id2,
               [_backtest_entry(tmp2, artifact_hash2)])
    loop_min._finalize_gate_failed(
        conn, ctx=ctx2, backlog_id=backlog_id2, reason="gate_failed:2",
        now=NOW)

    text = _index_path(loop_min).read_text()
    assert text.count("| date | mission | status |") == 1


def _second_mission(conn):
    from agentic_fx.store import improve_runs as improve_runs_store
    from agentic_fx.store import missions as missions_store
    backlog_id = backlog_store.add(conn, "idea2", "user", NOW)
    mission_id = missions_store.start(
        conn, "improve", "local", "model", NOW, commit=False)
    run_id = improve_runs_store.start(
        conn, backlog_id, NOW, mission_id=mission_id, commit=False)
    conn.commit()
    return mission_id, run_id, backlog_id


def test_index_write_failure_does_not_break_termination(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """`OSError` (readonly dir 等、ここは INDEX.md 自体の open 失敗を模す)
    は activity `archive_index_failed` のみで終端を壊さない — ledger 永続化
    (archive publish 含む) は commit 成功済みなので、mission_outcome 行は
    通常どおり書かれていること。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])
    monkeypatch.setattr(
        loop_min, "_append_archive_index",
        lambda *a, **kw: (_ for _ in ()).throw(
            OSError(13, "Permission denied")))

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="gate_failed:test",
        now=NOW)

    assert conn.execute(
        "SELECT mission_outcome FROM backtest_runs WHERE mission_id=?",
        (mission_id,)).fetchone()[0] == "gate_failed"
    assert not _index_path(loop_min).exists()
    assert "archive_index_failed" in loop_min._activity._path.read_text()
