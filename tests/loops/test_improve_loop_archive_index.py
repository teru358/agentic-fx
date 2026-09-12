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
    """ローカル T4 1 周目 #L4 (2026-09-10) を同居: INDEX 行の日付セルは
    `now.isoformat()` (ブリーフ「変更点」3 の `| <now ISO> |`)。`str(now)`
    に落とすと空白区切りになり ISO 形式でなくなる。"""
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


def test_index_concurrent_first_append_writes_single_header(loop_min):
    """codex 1 周目 (T3+T4) Important 1 (2026-09-11): 2 mission の初回追記が
    並行しても、ヘッダは 1 組・各 mission 1 行 (プロセス内 lock で直列化)。
    lock を外すと両方が「ファイル無し」を観測しヘッダが 2 組になる。"""
    import threading
    from types import SimpleNamespace
    rows = [{"id": 1, "name": "x", "artifact_hash": "a" * 64,
             "archive_path": "plugins/_archive/1/" + "a" * 64,
             "metrics": {"pf": 1.0, "trades": 5, "evaluable": True}}]
    barrier = threading.Barrier(2)
    real_exists = type(_index_path(loop_min)).exists

    def slow_exists(self, *a, **kw):
        result = real_exists(self, *a, **kw)
        if self.name == "INDEX.md":
            try:
                barrier.wait(timeout=2)  # 両者が exists() を評価し終えるまで待つ
            except threading.BrokenBarrierError:
                pass
        return result

    import pathlib as _pl
    orig = _pl.Path.exists
    _pl.Path.exists = slow_exists
    try:
        threads = [threading.Thread(
            target=loop_min._append_archive_index,
            args=(SimpleNamespace(mission_id=m),),
            kwargs=dict(status="failed", rows=rows, now=NOW)) for m in (1, 2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(5)
    finally:
        _pl.Path.exists = orig
    lines = _index_path(loop_min).read_text().splitlines()
    assert sum(ln.startswith("| date |") for ln in lines) == 1
    assert sum("| mission 1 |" in ln for ln in lines) == 1
    assert sum("| mission 2 |" in ln for ln in lines) == 1


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


def test_finalize_success_writes_index_row_for_approval(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """approval-quality 設計書 §B (2026-09-12、[archive-index-naming]):
    approval 終端も INDEX.md へ 1 行残す (旧稿は「approval は書かない」を
    pin していたが、A4 12〜13 回目の観測 B — approval が 3 run 連続で
    INDEX に載らない — を受けて挙動を反転する)。行フォーマットは既存の
    report/observation/failed 系と同じ列 (mission/status/candidates/best/
    archive) を共有する。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)

    text = _index_path(loop_min).read_text()
    assert f"mission {mission_id}" in text
    assert "| approval |" in text
    assert f"best=x@{artifact_hash[:8]}" in text
    assert "candidates=1" in text


def test_finalize_success_index_row_matches_other_terminal_columns(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """approval 行は report/observation/failed 系と同じ列見出しを共有する
    (別のフォーマットの表を新設しない)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)

    text = _index_path(loop_min).read_text()
    assert text.count("| date | mission | status | candidates | best | "
                      "archive |") == 1


def test_finalize_success_index_header_notes_it_is_a_terminal_log(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """設計書 §B: 「終端ログ (GC 非対象)。正は candidate_archives」の趣旨が
    ヘッダに明記される。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)

    text = _index_path(loop_min).read_text()
    # ローカル approval-quality 1 周目 #B2 (2026-09-12): 2 語の部分一致では
    # 「(GC 非対象)」「承認可否の目録ではない」「approval_requests」が
    # 落ちる変異を検出できなかった。ヘッダ文言を一文ごと pin する。
    assert "終端ログ (GC 非対象)。承認可否の目録ではない。" in text
    assert ("承認済み候補の正は `candidate_archives` 表と "
            "`approval_requests` 表である。") in text


def test_finalize_success_does_not_write_index_row_with_zero_candidates(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """行 0 件 (ledger entries が run_backtest を含まない) の approval
    mission は INDEX 行を書かない (既存の「行 0 件は書かない」不変条件を
    approval 経路でも保つ)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    ctx = _ctx(tmp_path, mission_id, run_id, [])

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)

    assert not _index_path(loop_min).exists()


def test_finalize_success_index_row_not_duplicated_on_direct_call_without_ctx(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """`ctx=None` (直接呼び出し、単体テストが多用する形) でも `persist_ctx`
    (mission_id を持つ) 経由で INDEX 行が書ける — `ctx` 自体が `None` でも
    `AttributeError` にならないこと。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.record(**_backtest_entry(tmp, artifact_hash))
    ledger.freeze()

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=NOW, ledger_entries=tuple(ledger.entries()))

    text = _index_path(loop_min).read_text()
    assert f"mission {mission_id}" in text
    assert "| approval |" in text


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


def test_index_marker_requires_full_mission_cell(loop_min):
    """ローカル 2 周目 #I1 (2026-09-11): 既出判定の marker は
    `| mission <id> |` の**セル全体**でなければならない。末尾の `|` を落とす
    と `| mission 11 |` の行が mission 1 の marker (`| mission 1`) に部分一致
    し、mission 1 の行が永久に書かれなくなる (INDEX は 1 mission 1 行の唯一の索引で、
    落ちた行は誰も気づけない)。既存の重複テストは mission 1 と 2 しか使わず、
    `marker` から末尾 `|` を削る変異を緑で通していた (実測 SURVIVED)。"""
    from types import SimpleNamespace
    rows = [{"id": 1, "name": "x", "artifact_hash": "a" * 64,
             "archive_path": "plugins/_archive/1/" + "a" * 64,
             "metrics": {"pf": 1.0, "trades": 5, "evaluable": True}}]
    # 桁数の長い mission を先に書く = 短い側の marker が部分一致する向き
    for mission_id in (11, 1):
        loop_min._append_archive_index(
            SimpleNamespace(mission_id=mission_id), status="failed",
            rows=rows, now=NOW)
    lines = _index_path(loop_min).read_text().splitlines()
    assert sum("| mission 1 |" in ln for ln in lines) == 1
    assert sum("| mission 11 |" in ln for ln in lines) == 1
