from __future__ import annotations

from datetime import datetime, timezone

from agentic_fx.loops.improve_loop import _REPORT_WRITE_FAILED
from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.plugin.version_store import artifact_hash_bytes
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import db as db_mod
from agentic_fx.store import improve_runs as improve_runs_store
from agentic_fx.store import missions as missions_store


NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


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


def _backtest_entry(tmp, artifact_hash):
    return {
        "opaque_ref": f"bt:{tmp.name}", "kind": "run_backtest",
        "params": {"name": "x", "pair": "USDJPY"}, "trial_count": 1,
        "result_summary": {
            "scope": "in_sample", "plugin_ref": "plugins/x",
            "content_hash": "content", "kind": "strategy",
            "pair": "USDJPY", "timeframe": "1h", "source": "test",
            "base_interval": "1m", "period": (NOW, NOW), "metrics": {"pf": 1.2},
            "settings_hash": "settings", "core_commit": "core",
            "initial_balance": 10000.0, "now": NOW, "params": {},
            "artifact_hash": artifact_hash, "archive_tmp": str(tmp),
        },
    }


def test_publish_same_hash_race_keeps_one_row_and_removes_loser_tmp(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, _ = mission_and_run_fixture
    first = tmp_path / "first"
    second = tmp_path / "second"
    artifact_hash = _snapshot(first)
    assert _snapshot(second) == artifact_hash
    ctx = _ctx(tmp_path, mission_id, run_id, [
        _backtest_entry(first, artifact_hash),
        _backtest_entry(second, artifact_hash),
    ])

    conn.execute("BEGIN IMMEDIATE")
    ids = loop_min._persist_ledger_in_tx(
        conn, ctx=ctx, now=NOW, mission_outcome="failed")
    conn.commit()

    assert ids == []
    final = loop_min._root / "plugins" / "_archive" / str(mission_id) / artifact_hash
    assert final.is_dir()
    assert not first.exists() and not second.exists()
    assert conn.execute(
        "SELECT count(*) FROM candidate_archives WHERE mission_id=?",
        (mission_id,)).fetchone()[0] == 1


def test_publish_retry_revalidates_hash_before_inserting_row(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, _ = mission_and_run_fixture
    missing_tmp = tmp_path / "gone"
    artifact_hash = artifact_hash_bytes(b"expected", b"bytes", b"here")
    final = loop_min._root / "plugins" / "_archive" / str(mission_id) / artifact_hash
    _snapshot(final)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(missing_tmp, artifact_hash)])

    conn.execute("BEGIN IMMEDIATE")
    loop_min._persist_ledger_in_tx(
        conn, ctx=ctx, now=NOW, mission_outcome="failed")
    conn.commit()

    assert conn.execute(
        "SELECT count(*) FROM candidate_archives WHERE mission_id=?",
        (mission_id,)).fetchone()[0] == 0
    assert "archive_failed" in loop_min._activity._path.read_text()


def test_failed_terminal_persists_outcome_and_marks_ledger_persisted(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, _ = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_failed_mission(
        conn, ctx=ctx,
        result=MissionResult(status="failed", output=None, transcript=[]),
        now=NOW)

    assert conn.execute(
        "SELECT mission_outcome FROM backtest_runs WHERE mission_id=?",
        (mission_id,)).fetchone()[0] == "failed"
    assert ctx.ledger.state() == "PERSISTED"


def test_savepoint_failure_finishes_terminal_and_marks_persist_failed(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    mission_id, run_id, _ = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])
    from agentic_fx.store import backtest_runs
    monkeypatch.setattr(
        backtest_runs, "save_harness_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyError("injected")))

    loop_min._finalize_failed_mission(
        conn, ctx=ctx,
        result=MissionResult(status="failed", output=None, transcript=[]),
        now=NOW)

    assert conn.execute(
        "SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()[0] == "failed"
    assert conn.execute("SELECT count(*) FROM backtest_runs").fetchone()[0] == 0
    assert ctx.ledger.state() == "PERSIST_FAILED"
    assert "ledger_persist_failed" in loop_min._activity._path.read_text()


# ---------------------------------------------------------------------------
# 段0変異スイープ是正 (2026-09-10): T3-3/T3-7/T3-8/T3-9/T3-12/T3-13 の pin。
# §3 全終端表がテストの守る仕様 — 各終端が `backtest_runs.mission_outcome` に
# 書く文字列そのものと、error entry を数から除く `accepted_entries` の適用
# 範囲 (payload / activity 双方) をリテラルで固定する。
# ---------------------------------------------------------------------------


def test_finalize_gate_failed_persists_outcome_gate_failed_and_archives(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """T3-7 pin: `_finalize_gate_failed` は `mission_outcome='gate_failed'`
    で行を書く (mutant は文字列を `'approval'` にすり替える)。この経路で
    `archive_tmp` 付き entry を渡し、`candidate_archives` 行が揃うことも
    合わせて確認する (§3 表の要求 — 少なくとも1経路)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="gate_failed:test",
        now=NOW)

    row = conn.execute(
        "SELECT mission_outcome FROM backtest_runs WHERE mission_id=?",
        (mission_id,)).fetchone()
    assert row is not None
    assert row["mission_outcome"] == "gate_failed"
    assert ctx.ledger.state() == "PERSISTED"
    assert conn.execute(
        "SELECT count(*) FROM candidate_archives WHERE mission_id=?",
        (mission_id,)).fetchone()[0] == 1
    final = loop_min._root / "plugins" / "_archive" / str(mission_id) / artifact_hash
    assert final.is_dir()


def test_finalize_gate_failed_gate_rows_carry_outcome_and_stay_out_of_live(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """/code-review 2 周目 CR1 (2026-09-11): 親ゲート行 (`gate_rows`) も
    `mission_outcome='gate_failed'` で保存する。NULL のままだと
    `latest_in_sample_metrics` の live 絞りを通り、gate 不合格候補の成績が
    live plugin の成績として表示される (content_hash が同じ候補)。"""
    from agentic_fx.store import backtest_runs
    mission_id, run_id, backlog_id = mission_and_run_fixture
    ctx = _ctx(tmp_path, mission_id, run_id, [])
    live_hash = "h" * 64
    gate_row = dict(
        scope="in_sample", plugin_ref="x", content_hash=live_hash,
        kind="strategy", pair="USDJPY", timeframe="1h", source="test",
        base_interval="1m", period=(NOW, NOW), metrics={"pf": 9.9, "trades": 50},
        settings_hash="s", core_commit="c", initial_balance=1.0, now=NOW)
    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="gate_failed:test",
        now=NOW, gate_rows=[gate_row])
    rows = conn.execute(
        "SELECT mission_outcome FROM backtest_runs WHERE mission_id=? "
        "AND content_hash=?", (mission_id, live_hash)).fetchall()
    assert [r["mission_outcome"] for r in rows] == ["gate_failed"]
    assert backtest_runs.latest_in_sample_metrics(
        conn, live_hash, pair="USDJPY", variant="candidate", source="test",
        base_interval="1m") is None


def test_finalize_loser_persists_outcome_loser(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """§3 表: 敗者経路は `mission_outcome='loser'`。"""
    mission_id, run_id, _ = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_loser(
        conn, ctx=ctx, output={"selected": {"idea": "dup"}}, now=NOW)

    row = conn.execute(
        "SELECT mission_outcome FROM backtest_runs WHERE mission_id=?",
        (mission_id,)).fetchone()
    assert row is not None
    assert row["mission_outcome"] == "loser"
    assert ctx.ledger.state() == "PERSISTED"


def test_finalize_output_invalid_persists_outcome_output_invalid(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """§3 表: schema 不合格経路は `mission_outcome='output_invalid'`
    (T3-8 pin: mutant は `_persist_ledger_in_tx` を呼ばず `ledger_ids=[]`
    に差し替えるので行が一切書かれなくなる)。"""
    mission_id, run_id, _ = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_output_invalid(
        conn, ctx=ctx, reason="schema_invalid:missing name", now=NOW)

    row = conn.execute(
        "SELECT mission_outcome FROM backtest_runs WHERE mission_id=?",
        (mission_id,)).fetchone()
    assert row is not None
    assert row["mission_outcome"] == "output_invalid"
    assert ctx.ledger.state() == "PERSISTED"


def test_finalize_report_or_observation_persists_outcome_report(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """T3-13 pin: `report_path is not None` のとき `mission_outcome='report'`
    (mutant は report/observation の条件式を反転させる)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])
    reports_dir = loop_min._root / "data" / "improve_reports"
    (reports_dir / ".tmp").mkdir(parents=True, exist_ok=True)
    loop_min._write_report_part(
        reports_dir, mission_id=mission_id, body_md="body")
    final_path = loop_min._final_report_path(
        reports_dir, mission_id=mission_id, now=NOW)

    loop_min._finalize_report_or_observation(
        conn, ctx=ctx, backlog_id=backlog_id, report_path=str(final_path),
        artifact={"type": "report"}, now=NOW)

    row = conn.execute(
        "SELECT mission_outcome FROM backtest_runs WHERE mission_id=?",
        (mission_id,)).fetchone()
    assert row is not None
    assert row["mission_outcome"] == "report"
    assert ctx.ledger.state() == "PERSISTED"


def test_finalize_report_or_observation_persists_outcome_observation(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """T3-13 pin (逆方向): `report_path is None` のとき
    `mission_outcome='observation'`。上のテストと対で条件反転を検出する。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_report_or_observation(
        conn, ctx=ctx, backlog_id=backlog_id, report_path=None,
        artifact={"type": "observation", "reason": "insufficient data"},
        now=NOW)

    row = conn.execute(
        "SELECT mission_outcome FROM backtest_runs WHERE mission_id=?",
        (mission_id,)).fetchone()
    assert row is not None
    assert row["mission_outcome"] == "observation"
    assert ctx.ledger.state() == "PERSISTED"


def test_prepare_report_write_failure_persists_outcome_report_failed(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """§3 表: report `.part` 書込失敗 (OSError) は `mission_outcome=
    'report_failed'` で終端する。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])
    monkeypatch.setattr(
        loop_min, "_write_report_part",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("write failed")))

    result = loop_min._prepare_report_if_applicable(
        conn, ctx=ctx, artifact={"type": "report", "proposal_kind": "core",
                                 "body_md": "x"},
        output={}, now=NOW, backlog_id=backlog_id)

    assert result is _REPORT_WRITE_FAILED
    row = conn.execute(
        "SELECT mission_outcome FROM backtest_runs WHERE mission_id=?",
        (mission_id,)).fetchone()
    assert row is not None
    assert row["mission_outcome"] == "report_failed"
    assert ctx.ledger.state() == "PERSISTED"


def test_finalize_success_persists_outcome_approval(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """§3 表: 承認申請経路は `mission_outcome='approval'`。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)

    row = conn.execute(
        "SELECT mission_outcome FROM backtest_runs WHERE mission_id=?",
        (mission_id,)).fetchone()
    assert row is not None
    assert row["mission_outcome"] == "approval"
    assert ctx.ledger.state() == "PERSISTED"


def test_finalize_success_payload_counts_exclude_error_entries(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """T3-3 pin: `_finalize_success` の approval payload `trial_count`/
    `analysis_call_count` は `accepted_entries` (error entry 除外) から
    計算する (mutant は `list(ledger_entries)` にすり替え、error entry の
    `trial_count` まで足し込む)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    entries = [
        {"opaque_ref": "a1", "kind": "analyze_corr", "params": {},
         "trial_count": 5,
         "result_summary": {"params": {}, "trial_count": 5,
                            "source": "improve_agent"}},
        {"opaque_ref": "a2", "kind": "analyze_corr", "params": {},
         "trial_count": 30,
         "result_summary": {"error": "insufficient_data"}},
    ]
    ctx = _ctx(tmp_path, mission_id, run_id, entries)

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)

    import json as _json
    row = conn.execute(
        "SELECT payload_json FROM approval_requests WHERE kind='plugin' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    payload = _json.loads(row["payload_json"])
    assert payload["trial_count"] == 5
    assert payload["analysis_call_count"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 1


def test_finalize_failed_mission_skips_error_entries_and_logs_activity(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """T3-12 pin: `_persist_ledger_in_tx` は素の `ctx.ledger.entries()`
    (error entry を含む) を `_persist_ledger_rows` に渡し、そちら側で
    `ledger_entry_skipped_error` activity を書いてから accepted のみを
    保存する。mutant は呼び出し側で先に `accepted_entries` を適用するため、
    `_persist_ledger_rows` の error-detection ループが空になり activity が
    書かれなくなる。"""
    mission_id, run_id, _ = mission_and_run_fixture
    entries = [
        {"opaque_ref": "a1", "kind": "analyze_corr", "params": {},
         "trial_count": 0,
         "result_summary": {"error": "insufficient_data"}},
    ]
    ctx = _ctx(tmp_path, mission_id, run_id, entries)

    loop_min._finalize_failed_mission(
        conn, ctx=ctx,
        result=MissionResult(status="failed", output=None, transcript=[]),
        now=NOW)

    assert conn.execute(
        "SELECT count(*) FROM analysis_runs").fetchone()[0] == 0
    activity_text = loop_min._activity._path.read_text()
    assert "ledger_entry_skipped_error" in activity_text
    assert ctx.ledger.state() == "PERSISTED"


def _compensation_ctx(staging_dir, *, mission_id, run_id, tmp=None,
                      artifact_hash=None):
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    if tmp is not None:
        ledger.record(**_backtest_entry(tmp, artifact_hash))
    ledger.freeze()
    return ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=staging_dir / "_snapshot_src",
        allowed_backlog_ids=None, slot_key=None, ledger=ledger,
        rpc_handlers={})


def test_compensate_commit_failure_double_call_frozen_does_not_duplicate_rows(
        loop_no_seam, tmp_path, clock):
    """T3-9 pin: `compensate_commit_failure` の 2 回目呼び出し (別 ctx/ledger
    だが同一 mission、両方 FROZEN で開始 — プロセス再起動を跨いだ2重呼び出し
    を模す) は `already_saved` の DB 側チェックで永続化をスキップし、行数が
    増えないこと。mutant (`if True:`) は毎回 `_persist_ledger_in_tx` を
    再実行し重複行を作る。"""
    loop, db_path = loop_no_seam
    conn = db_mod.connect(db_path)
    backlog_id = backlog_store.add(conn, "idea", "user", clock.now())
    mission_id = missions_store.start(
        conn, "improve", "local", "model", clock.now(), commit=False)
    run_id = improve_runs_store.start(
        conn, backlog_id, clock.now(), mission_id=mission_id, commit=False)
    conn.commit()
    conn.close()

    staging_dir = tmp_path / "plugins" / "_staging" / str(mission_id)
    staging_dir.mkdir(parents=True)
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx1 = _compensation_ctx(staging_dir, mission_id=mission_id, run_id=run_id,
                             tmp=tmp, artifact_hash=artifact_hash)

    loop.compensate_commit_failure(ctx=ctx1, now=clock.now(),
                                   exc=RuntimeError("boom"))

    check = db_mod.connect(db_path)
    assert check.execute(
        "SELECT count(*) FROM backtest_runs WHERE mission_id=? "
        "AND mission_outcome='commit_failed'", (mission_id,)).fetchone()[0] == 1
    check.close()
    assert ctx1.ledger.state() == "PERSISTED"

    # 2回目: 新しい ctx/ledger だが同じ tmp を指す entry (再起動後の再構築を
    # 模す) — `already_saved` の DB 照会だけが重複を防ぐ唯一の防波堤。
    ctx2 = _compensation_ctx(staging_dir, mission_id=mission_id, run_id=run_id,
                             tmp=tmp, artifact_hash=artifact_hash)
    loop.compensate_commit_failure(ctx=ctx2, now=clock.now(),
                                   exc=RuntimeError("boom"))

    check = db_mod.connect(db_path)
    assert check.execute(
        "SELECT count(*) FROM backtest_runs WHERE mission_id=? "
        "AND mission_outcome='commit_failed'", (mission_id,)).fetchone()[0] == 1
    assert check.execute(
        "SELECT count(*) FROM candidate_archives WHERE mission_id=?",
        (mission_id,)).fetchone()[0] == 1
    check.close()


def test_compensate_commit_failure_double_call_writes_index_row_once(
        loop_no_seam, tmp_path, clock):
    """codex 1 周目 (T3+T4) Important 1 (2026-09-11): 外部補償の二重呼び出し
    (同 ctx、1 回目で PERSISTED) でも INDEX.md の行は 1 mission 1 行。"""
    loop, db_path = loop_no_seam
    conn = db_mod.connect(db_path)
    backlog_id = backlog_store.add(conn, "idea", "user", clock.now())
    mission_id = missions_store.start(
        conn, "improve", "local", "model", clock.now(), commit=False)
    run_id = improve_runs_store.start(
        conn, backlog_id, clock.now(), mission_id=mission_id, commit=False)
    conn.commit()
    conn.close()
    staging_dir = tmp_path / "plugins" / "_staging" / str(mission_id)
    staging_dir.mkdir(parents=True)
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _compensation_ctx(staging_dir, mission_id=mission_id, run_id=run_id,
                            tmp=tmp, artifact_hash=artifact_hash)
    loop.compensate_commit_failure(ctx=ctx, now=clock.now(),
                                   exc=RuntimeError("boom"))
    assert ctx.ledger.state() == "PERSISTED"
    loop.compensate_commit_failure(ctx=ctx, now=clock.now(),
                                   exc=RuntimeError("boom again"))
    index = tmp_path / "plugins" / "_archive" / "INDEX.md"
    lines = index.read_text().splitlines()
    assert sum(f"| mission {mission_id} |" in ln for ln in lines) == 1
    assert sum(ln.startswith("| date |") for ln in lines) == 1


def test_compensate_commit_failure_savepoint_failure_then_terminal_insert_only_once(
        loop_no_seam, tmp_path, clock, monkeypatch):
    """§9 M2 pin: 1回目 SAVEPOINT 失敗 (`save_harness_run` 例外) →
    `PERSIST_FAILED` (mission は終端)。2回目 (mission は既に terminal) は
    行だけ挿入し `PERSISTED` に遷移。3回目は増えない。"""
    loop, db_path = loop_no_seam
    conn = db_mod.connect(db_path)
    backlog_id = backlog_store.add(conn, "idea", "user", clock.now())
    mission_id = missions_store.start(
        conn, "improve", "local", "model", clock.now(), commit=False)
    run_id = improve_runs_store.start(
        conn, backlog_id, clock.now(), mission_id=mission_id, commit=False)
    conn.commit()
    conn.close()

    staging_dir = tmp_path / "plugins" / "_staging" / str(mission_id)
    staging_dir.mkdir(parents=True)
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _compensation_ctx(staging_dir, mission_id=mission_id, run_id=run_id,
                            tmp=tmp, artifact_hash=artifact_hash)

    from agentic_fx.store import backtest_runs
    real_save = backtest_runs.save_harness_run
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("injected")
        return real_save(*a, **kw)

    monkeypatch.setattr(backtest_runs, "save_harness_run", flaky)

    loop.compensate_commit_failure(ctx=ctx, now=clock.now(),
                                   exc=RuntimeError("boom"))
    assert ctx.ledger.state() == "PERSIST_FAILED"
    check = db_mod.connect(db_path)
    assert check.execute(
        "SELECT count(*) FROM backtest_runs WHERE mission_id=? "
        "AND mission_outcome='commit_failed'", (mission_id,)).fetchone()[0] == 0
    assert check.execute(
        "SELECT status FROM missions WHERE id=?",
        (mission_id,)).fetchone()["status"] == "failed"
    check.close()

    loop.compensate_commit_failure(ctx=ctx, now=clock.now(),
                                   exc=RuntimeError("boom"))
    assert ctx.ledger.state() == "PERSISTED"
    check = db_mod.connect(db_path)
    assert check.execute(
        "SELECT count(*) FROM backtest_runs WHERE mission_id=? "
        "AND mission_outcome='commit_failed'", (mission_id,)).fetchone()[0] == 1
    check.close()

    loop.compensate_commit_failure(ctx=ctx, now=clock.now(),
                                   exc=RuntimeError("boom"))
    check = db_mod.connect(db_path)
    assert check.execute(
        "SELECT count(*) FROM backtest_runs WHERE mission_id=? "
        "AND mission_outcome='commit_failed'", (mission_id,)).fetchone()[0] == 1
    check.close()


def _snapshot_real(path):
    """実物の tmp snapshot と同じ 4 ファイル構成 (`meta.json` 込み) を作る。

    `artifact_hash_bytes` は 3 ファイルだけを対象にする契約なので、
    `meta.json` は hash に寄与しない (metrics を含み実行ごとに変わるため)。
    """
    import json as _json
    path.mkdir(parents=True)
    blobs = (b"plugin = 1\n", b"name: x\n", b"def test_x(): pass\n")
    for name, data in zip(("plugin.py", "config.yaml", "test_plugin.py"), blobs):
        (path / name).write_bytes(data)
    (path / "meta.json").write_text(
        _json.dumps({"pair": "USDJPY", "metrics": {"pf": 1.2}}))
    return artifact_hash_bytes(*blobs)


def test_publish_retry_with_matching_hash_inserts_row_without_tmp(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """ローカル T3 1 周目 #Y1 (2026-09-10): 再試行の**成功**側 — tmp 無し +
    final あり (実物どおり `meta.json` 込み) + 再計算 hash 一致 なら
    「publish 済み」として `candidate_archives` に行だけを挿入する。

    既存 pin は hash **不一致**側 (`archive_failed`) しか踏んでいないため、
    再試行分岐を常に失敗させる変異 (`if final_path.is_symlink() or not
    final_path.is_dir():` → `if True:`) がフルスイートでも生存していた。
    """
    mission_id, run_id, _ = mission_and_run_fixture
    missing_tmp = tmp_path / "gone"
    assert not missing_tmp.exists()
    final = loop_min._root / "plugins" / "_archive" / str(mission_id)
    artifact_hash = _snapshot_real(final / "PLACEHOLDER")
    (final / "PLACEHOLDER").rename(final / artifact_hash)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(missing_tmp, artifact_hash)])

    conn.execute("BEGIN IMMEDIATE")
    loop_min._persist_ledger_in_tx(
        conn, ctx=ctx, now=NOW, mission_outcome="failed")
    conn.commit()

    row = conn.execute(
        "SELECT archive_path, artifact_hash FROM candidate_archives "
        "WHERE mission_id=?", (mission_id,)).fetchone()
    assert row is not None, "hash 一致の再試行は行を挿入する"
    assert row["artifact_hash"] == artifact_hash
    assert row["archive_path"] == (
        f"plugins/_archive/{mission_id}/{artifact_hash}")
    log = loop_min._activity._path
    assert "archive_failed" not in (log.read_text() if log.exists() else "")


def test_publish_fsyncs_final_parent_after_rename(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """ローカル T3 1 周目 #Y7 (2026-09-10): rename 成功のあと final の親 dir を
    `_fsync_dir` する (ブリーフ 3)。呼び出しを削る変異が生存していた。"""
    from agentic_fx.plugin import version_store

    calls = []
    monkeypatch.setattr(version_store, "_fsync_dir", lambda p: calls.append(p))

    mission_id, run_id, _ = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])

    conn.execute("BEGIN IMMEDIATE")
    loop_min._persist_ledger_in_tx(
        conn, ctx=ctx, now=NOW, mission_outcome="failed")
    conn.commit()

    final = loop_min._root / "plugins" / "_archive" / str(mission_id) / artifact_hash
    assert final.is_dir()
    assert calls == [final.parent]


def test_remove_archive_tmp_does_not_chmod_through_symlink(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """ローカル T3 1 周目 #Y8 (2026-09-10): EEXIST 敗者の tmp を消すとき、
    tmp 内の symlink を**辿らない** (`if not candidate.is_symlink()`)。

    `os.walk` は symlink-to-file を `filenames` に入れるので、ガードを外すと
    `chmod(0o600)` がリンク**先** (tmp の外のファイル) の mode を書き換える。
    symlink を含む tmp を作るテストが無く、この変異が生存していた。
    """
    mission_id, run_id, _ = mission_and_run_fixture
    outsider = tmp_path / "outside.txt"
    outsider.write_text("do not touch\n")
    outsider.chmod(0o644)

    winner = tmp_path / "winner"
    loser = tmp_path / "loser"
    artifact_hash = _snapshot(winner)
    assert _snapshot(loser) == artifact_hash
    (loser / "link_to_outsider").symlink_to(outsider)

    ctx = _ctx(tmp_path, mission_id, run_id, [
        _backtest_entry(winner, artifact_hash),
        _backtest_entry(loser, artifact_hash),
    ])

    conn.execute("BEGIN IMMEDIATE")
    loop_min._persist_ledger_in_tx(
        conn, ctx=ctx, now=NOW, mission_outcome="failed")
    conn.commit()

    assert not loser.exists(), "敗者 tmp は削除される"
    assert outsider.exists(), "symlink の先は削除されない"
    assert (outsider.stat().st_mode & 0o777) == 0o644, (
        "symlink を辿って chmod してはならない")


def test_finalize_success_begin_failure_still_runs_compensation(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """ローカル T3 1 周目 申し送り (2026-09-10): `BEGIN IMMEDIATE` 自体が例外
    (ロック競合) でも `_compensate_tx2_failure` は呼ばれる。旧実装は
    `persist_ctx` を tx の中で束縛していたため UnboundLocalError で補償が
    静かに飛び、mission が running のまま残った。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    ctx = _ctx(tmp_path, mission_id, run_id, [])
    import sqlite3

    class _FlakyBegin:
        """sqlite3.Connection の execute は差し替え不可 (read-only) なので
        委譲 proxy で最初の BEGIN IMMEDIATE だけを失敗させる。"""
        def __init__(self, inner):
            self._inner = inner
            self._armed = True

        def execute(self, sql, *args):
            if self._armed and sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                self._armed = False  # 補償 tx の BEGIN は通す
                raise sqlite3.OperationalError("database is locked (injected)")
            return self._inner.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    loop_min._finalize_success(
        _FlakyBegin(conn), mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)
    assert conn.execute(
        "SELECT status FROM missions WHERE id=?",
        (mission_id,)).fetchone()["status"] == "failed", (
        "BEGIN 失敗でも補償 tx で mission は failed に終端されなければならない")


def test_finalize_success_fails_tx2_when_ledger_persistence_fails(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """ローカル T3 1 周目 #Y2 (2026-09-10): approval 経路は SAVEPOINT 永続化が
    失敗したら Tx-2 全体を落とす (設計 §3「approval Tx-2 失敗 → 内部補償」)。

    `raise RuntimeError` を落とす変異は、監査値 (`analysis_run_ids` /
    `trial_count`) を欠いたまま `approval_requests` を commit してしまうが、
    既存テストは成功側しか踏んでおらずフルスイートでも生存していた。
    """
    from agentic_fx.store import backtest_runs

    mission_id, run_id, backlog_id = mission_and_run_fixture
    tmp = tmp_path / "archive"
    artifact_hash = _snapshot(tmp)
    ctx = _ctx(tmp_path, mission_id, run_id,
               [_backtest_entry(tmp, artifact_hash)])
    monkeypatch.setattr(
        backtest_runs, "save_harness_run",
        lambda *a, **kw: (_ for _ in ()).throw(KeyError("injected")))

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)

    assert conn.execute(
        "SELECT count(*) FROM approval_requests").fetchone()[0] == 0, (
        "台帳が永続化できないまま承認申請を出してはならない")
    assert conn.execute(
        "SELECT status FROM missions WHERE id=?",
        (mission_id,)).fetchone()["status"] == "failed"
    assert ctx.ledger.state() == "PERSIST_FAILED"
