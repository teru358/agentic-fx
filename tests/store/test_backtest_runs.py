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

from tests.backtest.factories import H, DATASET_1M


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_backtest_runs_issuer_and_view(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(H, H), metrics={"trades": 0}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    backtest_runs.save_harness_run(conn, scope="holdout_gate", **kw)
    backtest_runs.save_human_run(conn, **kw)     # scope は内部で human_custom 固定
    rows = backtest_runs.in_sample_view(conn)
    assert len(rows) == 1 and rows[0]["issued_by"] == "harness"
    assert {"period_start", "period_end"}.isdisjoint(rows[0].keys())  # 遮断 1
    assert rows[0]["base_interval"] == "1m"


def test_latest_in_sample_metrics_filters_by_dataset_interval(tmp_path):
    conn = _conn(tmp_path)
    common = dict(plugin_ref="p", content_hash="h", kind="strategy", pair="USDJPY",
                  timeframe="1h", source="dukascopy", period=(H, H),
                  settings_hash="s", core_commit="c", initial_balance=1.0, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", base_interval="1m",
                                   metrics={"trades": 1}, **common)
    backtest_runs.save_harness_run(conn, scope="in_sample", base_interval="5m",
                                   metrics={"trades": 5}, **common)
    assert backtest_runs.latest_in_sample_metrics(
        conn, "h", pair="USDJPY", variant="candidate", source="dukascopy",
        base_interval="1m") == {"trades": 1}


def test_human_run_cannot_forge_in_sample(tmp_path):
    """human 面から in_sample を偽装できない (API 形状 + DB CHECK の二重)。"""
    conn = _conn(tmp_path)
    with pytest.raises(TypeError):
        backtest_runs.save_human_run(conn, scope="in_sample",
                                     plugin_ref="p", content_hash="h",
                                     kind="strategy", pair="USDJPY",
                                     timeframe="1h", source="dukascopy", base_interval="1m",
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
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
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
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
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
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
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
              timeframe="1h", source="dukascopy", base_interval="1m", period=(H, H),
              metrics={"trades": 0}, settings_hash="s", core_commit="c",
              initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", pair="USDJPY", **kw)
    backtest_runs.save_harness_run(conn, scope="in_sample", pair="EURUSD", **kw)
    rows = backtest_runs.in_sample_view(conn, pair="EURUSD")
    assert len(rows) == 1 and rows[0]["pair"] == "EURUSD"


def test_in_sample_view_expands_metrics_json(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
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
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
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
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(H, H), metrics={"trades": 0}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    rows = backtest_runs.in_sample_view(conn)
    assert "created_at" not in rows[0]


def test_naive_period_rejected(tmp_path):
    conn = _conn(tmp_path)
    naive = datetime(2026, 7, 22, 12, 0)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(naive, H), metrics={}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=H)
    with pytest.raises(ValueError):
        backtest_runs.save_harness_run(conn, scope="in_sample", **kw)


def test_naive_now_rejected(tmp_path):
    conn = _conn(tmp_path)
    naive = datetime(2026, 7, 22, 12, 0)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(H, H), metrics={}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=naive)
    with pytest.raises(ValueError):
        backtest_runs.save_harness_run(conn, scope="in_sample", **kw)


def test_reversed_period_rejected(tmp_path):
    """裁定 D3① (2026-08-24): `period[0] > period[1]` (期間逆転) を
    fail-closed で拒否する。`period[0] == period[1]` (既存テスト群が使う
    ゼロ幅期間) は引き続き許容する。"""
    from datetime import timedelta
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(H, H - timedelta(hours=1)), metrics={}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=H)
    with pytest.raises(ValueError, match="period"):
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


# ---- ⑥latest_in_sample_metrics (プラン 7 Task 9) ----------------------------
#
# get_signals ツールが strategy 行に成績を添付するための面。in_sample_view
# は content_hash 絞りを持たず created_at も返さないため流用できない
# (opus R2 M8) — scope='in_sample' AND issued_by='harness' AND
# content_hash=? AND pair=? に絞り、最新判定は id 降順で行う。
#
# fix round 1 F2 (sonnet 実証): content_hash はコード由来で pair 非依存。
# 多 pair strategy は同一 hash で pair ごとに行ができるため、pair 絞りが
# 無いと他 pair の成績が誤帰属される — シグネチャに pair を必須化した。

def test_latest_in_sample_metrics_returns_none_when_no_match(tmp_path):
    conn = _conn(tmp_path)
    assert backtest_runs.latest_in_sample_metrics(
        conn, "nope", pair="USDJPY", variant="candidate", source="dukascopy",
        base_interval="1m") is None


def test_latest_in_sample_metrics_filters_content_hash(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", kind="strategy", pair="USDJPY",
              timeframe="1h", source="dukascopy", base_interval="1m", period=(H, H),
              settings_hash="s", core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(
        conn, scope="in_sample", content_hash="a", metrics={"trades": 1}, **kw)
    backtest_runs.save_harness_run(
        conn, scope="in_sample", content_hash="b", metrics={"trades": 2}, **kw)
    assert backtest_runs.latest_in_sample_metrics(
        conn, "a", pair="USDJPY", variant="candidate", source="dukascopy",
        base_interval="1m") == {"trades": 1}
    assert backtest_runs.latest_in_sample_metrics(
        conn, "b", pair="USDJPY", variant="candidate", source="dukascopy",
        base_interval="1m") == {"trades": 2}


def test_latest_in_sample_metrics_picks_latest_by_id_desc(tmp_path):
    """同一 content_hash に複数行あるとき、id が最大 (最新挿入) の行を返す。
    created_at (H 固定) が同一でも id で一意に決まることを検証する。"""
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(H, H), settings_hash="s", core_commit="c",
              initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(
        conn, scope="in_sample", metrics={"trades": 1}, **kw)
    backtest_runs.save_harness_run(
        conn, scope="in_sample", metrics={"trades": 99}, **kw)
    assert backtest_runs.latest_in_sample_metrics(
        conn, "h", pair="USDJPY", variant="candidate", source="dukascopy",
        base_interval="1m") == {"trades": 99}


def test_latest_in_sample_metrics_whitelists_metric_keys(tmp_path):
    """metrics に period_start 等を混入しても密輸できない (in_sample_view
    と同じ濾過境界)。"""
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(H, H),
              metrics={"trades": 40, "period_start": "2020-01-01T00:00:00+00:00"},
              settings_hash="s", core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    metrics = backtest_runs.latest_in_sample_metrics(
        conn, "h", pair="USDJPY", variant="candidate", source="dukascopy",
        base_interval="1m")
    assert metrics == {"trades": 40}
    assert "period_start" not in metrics


def test_latest_in_sample_metrics_excludes_holdout_gate_and_human_custom(tmp_path):
    """scope='in_sample' AND issued_by='harness' 以外は対象外。"""
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(H, H), metrics={"trades": 1}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="holdout_gate", **kw)
    backtest_runs.save_human_run(conn, **kw)
    assert backtest_runs.latest_in_sample_metrics(
        conn, "h", pair="USDJPY", variant="candidate", source="dukascopy",
        base_interval="1m") is None


# ---- fix round 1 F1 (Critical, codex): ネスト密輸の遮断 ---------------------
#
# METRIC_KEYS の白リスト濾過はトップレベルキーしか見ていなかったため、
# 白リストキーの値の中に {"trades": {"period_start": ...}} のようにネスト
# して holdout 期間を密輸できた。値がスカラー (int/float/bool/None/str) の
# ものだけを通すよう強化し、in_sample_view / latest_in_sample_metrics 両方
# で確認する (濾過ヘルパを共有しているため両方で同一の欠陥・同一の修正)。

def test_in_sample_view_filters_nested_dict_value_smuggling(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(H, H),
              metrics={"trades": {"period_start": "2020-01-01T00:00:00+00:00",
                                  "period_end": "2020-06-01T00:00:00+00:00"},
                      "pf": 1.5},
              settings_hash="s", core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    rows = backtest_runs.in_sample_view(conn)
    assert rows[0]["metrics"] == {"pf": 1.5}  # ネストした trades は落ちる
    assert "trades" not in rows[0]["metrics"]


def test_latest_in_sample_metrics_filters_nested_dict_value_smuggling(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(H, H),
              metrics={"trades": {"period_start": "2020-01-01T00:00:00+00:00"},
                      "pf": 1.5},
              settings_hash="s", core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    metrics = backtest_runs.latest_in_sample_metrics(
        conn, "h", pair="USDJPY", variant="candidate", source="dukascopy",
        base_interval="1m")
    assert metrics == {"pf": 1.5}
    assert "trades" not in metrics


def test_latest_in_sample_metrics_filters_list_value_smuggling(tmp_path):
    """値が list のネスト密輸も同様に落ちる (dict だけを弾く変異への防波堤)。"""
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(H, H),
              metrics={"trades": ["2020-01-01T00:00:00+00:00"], "pf": 1.5},
              settings_hash="s", core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    metrics = backtest_runs.latest_in_sample_metrics(
        conn, "h", pair="USDJPY", variant="candidate", source="dukascopy",
        base_interval="1m")
    assert metrics == {"pf": 1.5}


# ---- fix round 1 F2 (Important, sonnet): pair 帰属 --------------------------

def test_latest_in_sample_metrics_scoped_by_pair_not_content_hash_alone(tmp_path):
    """同一 content_hash (多 pair strategy) で pair ごとに成績を分離する。
    pair 絞りが無いと EURUSD 側の呼び出しに USDJPY の成績が付く
    (レビュアーが実 DB で再現した誤帰属)。"""
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="multi", kind="strategy",
              timeframe="1h", source="dukascopy", base_interval="1m", period=(H, H),
              settings_hash="s", core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(
        conn, scope="in_sample", pair="USDJPY", metrics={"pf": 1.1}, **kw)
    backtest_runs.save_harness_run(
        conn, scope="in_sample", pair="EURUSD", metrics={"pf": 2.2}, **kw)
    assert backtest_runs.latest_in_sample_metrics(
        conn, "multi", pair="USDJPY", variant="candidate", source="dukascopy",
        base_interval="1m") == {"pf": 1.1}
    assert backtest_runs.latest_in_sample_metrics(
        conn, "multi", pair="EURUSD", variant="candidate", source="dukascopy",
        base_interval="1m") == {"pf": 2.2}


# ---- fix round 1 F4 (Important, codex): metrics_json 破損の fail-open -------

def test_latest_in_sample_metrics_malformed_json_returns_none_fail_open(tmp_path):
    """DB 破損や手動行で metrics_json が壊れていても None を返す (例外を
    伝播させない) — get_signals が他の正常な行まで道連れにしないための
    fail-open。"""
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(H, H), metrics={"trades": 1}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    conn.execute("UPDATE backtest_runs SET metrics_json='not json' "
                "WHERE content_hash='h'")
    conn.commit()
    assert backtest_runs.latest_in_sample_metrics(
        conn, "h", pair="USDJPY", variant="candidate", source="dukascopy",
        base_interval="1m") is None


def test_latest_in_sample_metrics_malformed_json_logs_warning(tmp_path, caplog):
    """裁定 D3② (2026-08-24): fail-open の挙動 (None 返却) は維持しつつ、
    内部で何が起きたかを WARNING ログで観測可能にする — 既に実装済みの
    ログを直接 pin する (`caplog` で `_log.warning` 呼び出しを観測)。"""
    import logging
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
              period=(H, H), metrics={"trades": 1}, settings_hash="s",
              core_commit="c", initial_balance=1e6, now=H)
    backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    conn.execute("UPDATE backtest_runs SET metrics_json='not json' "
                "WHERE content_hash='h'")
    conn.commit()
    with caplog.at_level(logging.WARNING, logger="agentic_fx.store.backtest_runs"):
        backtest_runs.latest_in_sample_metrics(
            conn, "h", pair="USDJPY", variant="candidate", source="dukascopy",
            base_interval="1m")
    assert any("decode failed" in r.message for r in caplog.records)


# ---- 8-D: variant/ref_* + latest_in_sample_metrics candidate 限定 --------

def test_save_harness_run_accepts_variant_and_ref_fields(tmp_path):
    """variant/ref_plugin_ref/ref_content_hash を明示指定できる (既定は
    'candidate'/None/None — 既存呼び出しは無変更のまま動く)。

    L52 (8-D M3): 従来は ref_plugin_ref/ref_content_hash に None を渡し
    variant しか読み戻さなかったため、両列が INSERT から落ちても green
    だった。非 None 値を渡して読み戻す。"""
    conn = _conn(tmp_path)
    run_id = backtest_runs.save_harness_run(
        conn, scope="in_sample", plugin_ref="no_strategy:rsi_v2",
        content_hash="cand-hash", kind="strategy", pair="USDJPY",
        timeframe="1h", source="test", base_interval="1m", period=(H, H),
        metrics={"pf": 1.0}, settings_hash="s", core_commit="c",
        initial_balance=10000.0, now=H,
        variant="no_strategy", ref_plugin_ref="p.py", ref_content_hash="h")
    row = conn.execute(
        "SELECT variant, ref_plugin_ref, ref_content_hash FROM backtest_runs "
        "WHERE id=?", (run_id,)).fetchone()
    assert row["variant"] == "no_strategy"
    assert row["ref_plugin_ref"] == "p.py"
    assert row["ref_content_hash"] == "h"


def test_save_harness_run_default_variant_is_candidate(tmp_path):
    """既存呼び出し (variant を渡さない) は 'candidate' になる (回帰なし)。"""
    conn = _conn(tmp_path)
    run_id = backtest_runs.save_harness_run(
        conn, scope="in_sample", plugin_ref="rsi_v2",
        content_hash="h", kind="indicator", pair="USDJPY", timeframe="1h",
        source="test", base_interval="1m", period=(H, H), metrics={"pf": 1.0},
        settings_hash="s", core_commit="c", initial_balance=10000.0, now=H)
    row = conn.execute("SELECT variant FROM backtest_runs WHERE id=?",
                       (run_id,)).fetchone()
    assert row["variant"] == "candidate"


def test_latest_in_sample_metrics_ignores_baseline_and_no_strategy_rows(tmp_path):
    """§8.1-40: latest_in_sample_metrics は variant='candidate' に絞る
    (挙動変更、pin)。baseline 行が候補の content_hash と衝突しても無視する。"""
    conn = _conn(tmp_path)
    backtest_runs.save_harness_run(
        conn, scope="in_sample", plugin_ref="p", content_hash="h",
        kind="strategy", pair="USDJPY", timeframe="1h", source="test", base_interval="1m",
        period=(H, H), metrics={"pf": 9.9}, settings_hash="s",
        core_commit="c", initial_balance=10000.0, now=H, variant="baseline",
        ref_plugin_ref="p", ref_content_hash="h")  # 同じ content_hash で baseline 行
    got = backtest_runs.latest_in_sample_metrics(
        conn, "h", pair="USDJPY", variant="candidate", source="test",
        base_interval="1m")
    assert got is None  # candidate 行が無いので None (baseline は無視)

    backtest_runs.save_harness_run(
        conn, scope="in_sample", plugin_ref="p", content_hash="h",
        kind="strategy", pair="USDJPY", timeframe="1h", source="test", base_interval="1m",
        period=(H, H), metrics={"pf": 1.5}, settings_hash="s",
        core_commit="c", initial_balance=10000.0, now=H, variant="candidate")
    got2 = backtest_runs.latest_in_sample_metrics(
        conn, "h", pair="USDJPY", variant="candidate", source="test",
        base_interval="1m")
    assert got2["pf"] == 1.5  # candidate 行だけが返る


def test_save_harness_run_commit_false_does_not_commit(tmp_path):
    """直前修正の申し送り②: commit=False は conn.commit() を呼ばない —
    呼び出し元 (10.10節 Tx-2) が自分でロールバック可能な状態を保つ。"""
    conn = _conn(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    run_id = backtest_runs.save_harness_run(
        conn, scope="in_sample", plugin_ref="p", content_hash="h",
        kind="indicator", pair="USDJPY", timeframe="1h", source="test", base_interval="1m",
        period=(H, H), metrics={"pf": 1.0},
        settings_hash="s", core_commit="c", initial_balance=10000.0,
        now=H, commit=False)
    conn.rollback()
    row = conn.execute(
        "SELECT COUNT(*) c FROM backtest_runs WHERE id=?",
        (run_id,)).fetchone()
    assert row["c"] == 0   # rollback で消えている = commit されていなかった


def test_save_harness_run_accepts_mission_id_kw_and_persists_it(tmp_path):
    """プラン10 Task10-13 Step7 (RW4): Task 12 の `WHERE mission_id=?`
    assert が成立するための書込経路。"""
    conn = _conn(tmp_path)
    run_id = backtest_runs.save_harness_run(
        conn, scope="in_sample", plugin_ref="p", content_hash="h" * 8,
        kind="indicator", pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
        period=(datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 2, tzinfo=timezone.utc)),
        metrics={}, settings_hash="s", core_commit="c",
        initial_balance=10000.0, now=datetime(2026, 8, 22, tzinfo=timezone.utc),
        mission_id=42)
    row = conn.execute(
        "SELECT mission_id FROM backtest_runs WHERE id=?", (run_id,)).fetchone()
    assert row["mission_id"] == 42


def test_save_harness_run_mission_id_defaults_to_null(tmp_path):
    conn = _conn(tmp_path)
    run_id = backtest_runs.save_harness_run(
        conn, scope="in_sample", plugin_ref="p", content_hash="h" * 8,
        kind="indicator", pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
        period=(datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 2, tzinfo=timezone.utc)),
        metrics={}, settings_hash="s", core_commit="c",
        initial_balance=10000.0, now=datetime(2026, 8, 22, tzinfo=timezone.utc))
    row = conn.execute(
        "SELECT mission_id FROM backtest_runs WHERE id=?", (run_id,)).fetchone()
    assert row["mission_id"] is None


def test_save_harness_run_params_default_is_not_shared_mutable(tmp_path):
    """M1 (codex 段階2/3 是正 1周目 Minor): `params: dict = {}` の mutable
    default を排す。既定 (params 未指定) の複数保存間で、内部で使う辞書が
    同一オブジェクトを共有していないこと (将来 `_insert` 内で正規化処理を
    足しても run 間で汚染しない不変条件を pin する)。"""
    conn = _conn(tmp_path)
    kw = dict(plugin_ref="p.py", content_hash="h", kind="strategy",
              pair="USDJPY", timeframe="1h", source="dukascopy",
              base_interval="1m", period=(H, H), metrics={"trades": 0},
              settings_hash="s", core_commit="c", initial_balance=1e6, now=H)
    id1 = backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    id2 = backtest_runs.save_harness_run(conn, scope="in_sample", **kw)
    row1 = conn.execute("SELECT params_json FROM backtest_runs WHERE id=?",
                        (id1,)).fetchone()
    row2 = conn.execute("SELECT params_json FROM backtest_runs WHERE id=?",
                        (id2,)).fetchone()
    assert row1["params_json"] == row2["params_json"] == "{}"
    import inspect
    for fn in (backtest_runs._insert, backtest_runs.save_harness_run,
              backtest_runs.save_human_run):
        default = inspect.signature(fn).parameters["params"].default
        assert default is None, (
            f"{fn.__name__}.params の既定値は None であること (mutable "
            f"default {default!r} が残っている)")
