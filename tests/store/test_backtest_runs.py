"""backtest_runs のテスト (プラン 6 Task 8)。

上書き節 C/D/E (task-8-brief.md, コントローラ照合 2026-08-01) の契約:
- save_harness_run: scope は in_sample/holdout_gate のみ、issued_by は
  'harness' 固定。save_human_run: scope 引数を持たない (TypeError で偽装
  不可)、issued_by='human_cli' / scope='human_custom' 固定。
- in_sample_view: scope='in_sample' AND issued_by='harness' のみ、
  period_start/period_end は返却列に含めない (遮断 1)。
- period/now は aware datetime 必須 (naive は ValueError)。
- tests/store/ には共有 conftest が無い規約 — ローカル _conn(tmp_path)。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.store import backtest_runs
from agentic_fx.store.db import connect, init_db

from tests.backtest.conftest import H


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_backtest_runs_issuer_and_view(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy",
              period=(H, H), metrics={"trades": 0}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    backtest_runs.save_harness_run(conn, scope="holdout_gate", **kw)
    backtest_runs.save_human_run(conn, **kw)     # scope は内部で human_custom 固定
    rows = backtest_runs.in_sample_view(conn)
    assert len(rows) == 1 and rows[0]["issued_by"] == "harness"
    assert {"period_start", "period_end"}.isdisjoint(rows[0].keys())  # 遮断 1


def test_human_run_cannot_forge_in_sample(tmp_path):
    """human 面から in_sample を偽装できない (API 形状 + DB CHECK の二重)。"""
    conn = _conn(tmp_path)
    with pytest.raises(TypeError):
        backtest_runs.save_human_run(conn, scope="in_sample",
                                     plugin_ref="p", content_hash="h",
                                     kind="strategy", pair="USDJPY",
                                     timeframe="1h", source="dukascopy",
                                     period=(H, H), metrics={},
                                     settings_hash="s", core_commit="c",
                                     initial_balance=1e6, now=H)  # scope 引数を受けない
    with pytest.raises(sqlite3.IntegrityError):  # CHECK: 旧二値 'holdout' は不可
        conn.execute(
            "INSERT INTO backtest_runs (plugin_ref, content_hash, kind, pair,"
            " timeframe, source, period_start, period_end, scope, issued_by,"
            " metrics_json, settings_hash, core_commit, initial_balance,"
            " created_at) VALUES ('p','h','strategy','USDJPY','1h','dukascopy',"
            "'t','t','holdout','human_cli','{}','s','c',1,'t')")


def test_issued_by_check_rejects_unknown_value(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO backtest_runs (plugin_ref, content_hash, kind, pair,"
            " timeframe, source, period_start, period_end, scope, issued_by,"
            " metrics_json, settings_hash, core_commit, initial_balance,"
            " created_at) VALUES ('p','h','strategy','USDJPY','1h','dukascopy',"
            "'t','t','in_sample','robot','{}','s','c',1,'t')")


def test_save_harness_run_rejects_human_custom_scope(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy",
              period=(H, H), metrics={}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=H)
    with pytest.raises(ValueError):
        backtest_runs.save_harness_run(conn, scope="human_custom", **kw)


def test_in_sample_view_excludes_forged_issued_by_via_raw_insert(tmp_path):
    """scope='in_sample' の CHECK と issued_by='human_cli' の CHECK は
    独立に検査される (複合 CHECK ではない) — 生 INSERT で scope='in_sample'
    かつ issued_by='human_cli' の行を作れてしまう。この行を弾くのは
    save_* の API 形状ではなく in_sample_view の issued_by='harness' 条件
    (view 側の防御レイヤ)。"""
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO backtest_runs (plugin_ref, content_hash, kind, pair,"
        " timeframe, source, period_start, period_end, scope, issued_by,"
        " metrics_json, settings_hash, core_commit, initial_balance,"
        " created_at) VALUES ('p','h','strategy','USDJPY','1h','dukascopy',"
        "'t','t','in_sample','human_cli','{}','s','c',1,'t')")
    conn.commit()
    rows = backtest_runs.in_sample_view(conn)
    assert rows == []


def test_save_human_run_persists_human_custom_scope_and_issued_by(tmp_path):
    """save_human_run が実際に保存する行の scope/issued_by を検証する
    (シグネチャの TypeError テストだけでは、内部固定値が
    scope='in_sample' に化けても検出できない)。"""
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy",
              period=(H, H), metrics={}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=H)
    rowid = backtest_runs.save_human_run(conn, **kw)
    row = conn.execute(
        "SELECT scope, issued_by FROM backtest_runs WHERE id=?",
        (rowid,)).fetchone()
    assert (row["scope"], row["issued_by"]) == ("human_custom", "human_cli")


def test_in_sample_view_orders_by_created_at_then_id(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy",
              period=(H, H), metrics={}, settings_hash="s",
              core_commit="c", initial_balance=1e6)
    later = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    id_later = backtest_runs.save_harness_run(conn, scope="in_sample", now=later, **kw)
    id_earlier = backtest_runs.save_harness_run(conn, scope="in_sample", now=H, **kw)
    rows = backtest_runs.in_sample_view(conn)
    assert [r["id"] for r in rows] == [id_earlier, id_later]


def test_in_sample_view_filters_by_pair(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              timeframe="1h", source="dukascopy", period=(H, H),
              metrics={"trades": 0}, settings_hash="s", core_commit="c",
              initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", pair="USDJPY", **kw)
    backtest_runs.save_harness_run(conn, scope="in_sample", pair="EURUSD", **kw)
    rows = backtest_runs.in_sample_view(conn, pair="EURUSD")
    assert len(rows) == 1 and rows[0]["pair"] == "EURUSD"


def test_in_sample_view_expands_metrics_json(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy",
              period=(H, H), metrics={"pf": 1.5, "trades": 40},
              settings_hash="s", core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    rows = backtest_runs.in_sample_view(conn)
    assert rows[0]["metrics"] == {"pf": 1.5, "trades": 40}
    assert "metrics_json" not in rows[0]


def test_in_sample_view_whitelists_metric_keys_against_period_smuggling(tmp_path):
    """F1 (fix round 1, codex Important): metrics dict は save_harness_run
    の任意入力なので、"period_start"/"period_end" を metrics に混入すると
    列遮断 (holdout 遮断 1) を metrics_json 経由で密輸できてしまう。
    in_sample_view の白リスト濾過がそれを弾き、正規キーは残ることを検証。
    """
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy",
              period=(H, H),
              metrics={"trades": 40, "pf": 1.5,
                      "period_start": "2020-01-01T00:00:00+00:00",
                      "period_end": "2020-06-01T00:00:00+00:00"},
              settings_hash="s", core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    rows = backtest_runs.in_sample_view(conn)
    assert {"period_start", "period_end"}.isdisjoint(rows[0]["metrics"].keys())
    assert rows[0]["metrics"]["trades"] == 40
    assert rows[0]["metrics"]["pf"] == 1.5


def test_in_sample_view_excludes_created_at(tmp_path):
    """F2 (最終レビュー codex I1): created_at は返却列に含めない —
    run_in_sample が保存する created_at (= now_norm, 分格子切り捨て済み) を
    改善ループが読めると、holdout_months (既定値/設定/コードから既知) と
    合わせて holdout_boundary(created_at, holdout_months) で period_end を
    分精度で完全復元できてしまう (遮断 1 の派生漏洩)。"""
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy",
              period=(H, H), metrics={"trades": 0}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    rows = backtest_runs.in_sample_view(conn)
    assert "created_at" not in rows[0]


def test_naive_period_rejected(tmp_path):
    conn = _conn(tmp_path)
    naive = datetime(2026, 7, 22, 12, 0)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy",
              period=(naive, H), metrics={}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=H)
    with pytest.raises(ValueError):
        backtest_runs.save_harness_run(conn, scope="in_sample", **kw)


def test_naive_now_rejected(tmp_path):
    conn = _conn(tmp_path)
    naive = datetime(2026, 7, 22, 12, 0)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy",
              period=(H, H), metrics={}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=naive)
    with pytest.raises(ValueError):
        backtest_runs.save_harness_run(conn, scope="in_sample", **kw)


def test_settings_snapshot_hash_stable_and_sensitive_to_risk(tmp_path):
    from agentic_fx.config import load_settings
    from pathlib import Path
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" /
        "settings.yaml.example")
    h1 = backtest_runs.settings_snapshot_hash(settings)
    h2 = backtest_runs.settings_snapshot_hash(settings)
    assert h1 == h2 and isinstance(h1, str) and len(h1) == 64

    mutated = settings.model_copy(update={
        "risk": settings.risk.model_copy(
            update={"rr_min": settings.risk.rr_min + 1.0})})
    h3 = backtest_runs.settings_snapshot_hash(mutated)
    assert h3 != h1


def test_settings_snapshot_hash_sensitive_to_backtest_section():
    """F6 (fix round 1, sonnet M-1 killer): payload に backtest セクションが
    実際に含まれていること (backtest.initial_balance の変化でハッシュが
    変わること) を検証する — risk のみへの敏感性テストでは
    "backtest": ... の行を削除しても検出できない。"""
    from agentic_fx.config import load_settings
    from pathlib import Path
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" /
        "settings.yaml.example")
    h1 = backtest_runs.settings_snapshot_hash(settings)

    mutated = settings.model_copy(update={
        "backtest": settings.backtest.model_copy(
            update={"initial_balance":
                    settings.backtest.initial_balance + 1.0})})
    h2 = backtest_runs.settings_snapshot_hash(mutated)
    assert h2 != h1


def test_core_commit_success(monkeypatch):
    class _Result:
        returncode = 0
        stdout = "abc123\n"

    def _fake_run(*args, **kwargs):
        return _Result()

    monkeypatch.setattr(backtest_runs.subprocess, "run", _fake_run)
    assert backtest_runs.core_commit() == "abc123"


def test_core_commit_nonzero_returncode_is_unknown(monkeypatch):
    class _Result:
        returncode = 128
        stdout = ""

    def _fake_run(*args, **kwargs):
        return _Result()

    monkeypatch.setattr(backtest_runs.subprocess, "run", _fake_run)
    assert backtest_runs.core_commit() == "unknown"


def test_core_commit_exception_is_unknown(monkeypatch):
    def _fake_run(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(backtest_runs.subprocess, "run", _fake_run)
    assert backtest_runs.core_commit() == "unknown"
