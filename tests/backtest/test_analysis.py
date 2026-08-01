"""analysis — 相関 3 種・非漏洩出力契約・analysis_runs のテスト (プラン 6 Task 10)。

brief (task-10-brief.md Step 1) のテストを、上書き節 A (コントローラ照合
2026-08-02) の読み替えを適用して収録する:

- conftest の SETTINGS は pairs=["USDJPY"], datafeed.watch_symbols=[] な
  ので、EURUSD を使う analyze_for_agent 呼び出しは全て
  ``_settings_watch_eurusd()`` (ローカルヘルパ) を渡す
  (test_agent_output_contract_no_leak / test_agent_analysis_is_in_sample_bounded
  / test_analysis_runs_records_trials の 3 本)。
- ``_seed_two_series`` の既定 start=H (2026-07-22) は NOW (2026-08-01) の
  holdout boundary (2026-05-01, holdout_months=3) より後なので、
  analyze_for_agent の成功系テスト (no_leak / records_trials) では
  boundary より確実に前の start で明示的にシードする。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest.analysis import (
    LAGS, MIN_COMMON_OBS, TIMEFRAMES, WINDOWS, _load_returns, analyze_for_agent,
    corr_matrix, coverage_report, lead_lag, rolling_corr_summary,
)
from agentic_fx.store import ohlcv

from tests.backtest.conftest import H, SETTINGS, _conn

FAR_FUTURE = datetime(2030, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)
BEFORE_BOUNDARY = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _settings_watch_eurusd():
    """conftest の SETTINGS (watch_symbols=[]) を EURUSD 込みに拡張する。"""
    return SETTINGS.model_copy(update={
        "datafeed": SETTINGS.datafeed.model_copy(
            update={"watch_symbols": ["EURUSD"]})})


def _series(conn, symbol, values, *, start, timeframe="1h"):
    """決定的な close 列を 1h バーとして投入 (乱数・実時刻不使用)。"""
    step = timedelta(hours=1)
    rows = [(symbol, timeframe, (start + i * step).isoformat(),
             v, v + 0.05, v - 0.05, v, 1.0, 0.01)
            for i, v in enumerate(values)]
    ohlcv.import_bars(conn, rows, source="dukascopy")


def _sine(n, *, phase=0):
    """三角関数の決定的疑似価格列 (100 を中心に振幅 1)。"""
    return [100 + math.sin((i + phase) / 5.0) for i in range(n)]


def _seed_two_series(conn, *, start=H):
    """EURUSD が USDJPY に 1 バー先行する系列 (b[t] = a[t+1] と同位相差)。"""
    _series(conn, "USDJPY", _sine(200, phase=0), start=start)
    _series(conn, "EURUSD", _sine(200, phase=1), start=start)


def test_corr_matrix_inner_join_and_gap_exclusion(tmp_path):
    conn = _conn(tmp_path); _seed_two_series(conn)
    # USDJPY 側の 1 バーを欠損させても inner join で落ちるだけで計算は通る
    conn.execute("DELETE FROM ohlcv WHERE symbol='USDJPY' AND bar_time=?",
                 ((H + timedelta(hours=7)).isoformat(),)); conn.commit()
    m = corr_matrix(conn, ["USDJPY", "EURUSD"], timeframe="1h",
                    source="dukascopy", in_sample_until=FAR_FUTURE)
    assert ("USDJPY", "EURUSD") in m
    assert -1.0 <= m[("USDJPY", "EURUSD")] <= 1.0


def test_lead_lag_detects_leader(tmp_path):
    conn = _conn(tmp_path); _seed_two_series(conn)
    r = lead_lag(conn, "USDJPY", "EURUSD", timeframe="1h",
                 source="dukascopy", in_sample_until=FAR_FUTURE)
    assert set(r.keys()) == {"peak_lag", "peak_corr"}  # 返却スキーマ固定
    assert r["peak_lag"] == 1 and r["peak_corr"] > 0.9


def _leaves(x):
    """dict/list を再帰展開して (key 列, leaf 列) を返す。"""
    keys, leaves = [], []
    if isinstance(x, dict):
        for k, v in x.items():
            keys.append(k)
            k2, l2 = _leaves(v); keys += k2; leaves += l2
    elif isinstance(x, (list, tuple)):
        assert len(x) <= 8, "系列様の list を返してはならない"
        for v in x:
            k2, l2 = _leaves(v); keys += k2; leaves += l2
    else:
        leaves.append(x)
    return keys, leaves


def test_agent_output_contract_no_leak(tmp_path):
    """§6 出力契約: 日時・系列・観測数・端点を返さない (再帰チェック)。"""
    conn = _conn(tmp_path); _seed_two_series(conn, start=BEFORE_BOUNDARY)
    out = analyze_for_agent(conn, _settings_watch_eurusd(),
                            {"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD",
                             "timeframe": "1h"}, now=NOW)
    keys, leaves = _leaves(out)
    for leaf in leaves:
        assert not isinstance(leaf, datetime)
        assert not (isinstance(leaf, str) and len(leaf) >= 10
                    and leaf[4:5] == "-" and leaf[:4].isdigit())  # ISO 日付形
    assert "analysis_run_id" in keys
    forbidden = {"count", "n", "observations", "start", "end", "dates",
                 "period_start", "period_end", "window_series"}
    assert forbidden.isdisjoint(set(keys))


def test_agent_analysis_is_in_sample_bounded(tmp_path):
    """直近 (holdout 期) だけ逆位相の系列で、境界適用が効いていることを検証。"""
    conn = _conn(tmp_path)
    start = NOW - timedelta(days=200)          # in-sample 100 日 + holdout 100 日
    n_in, n_out = 2400, 2400                    # 1h バー数 (100 日ずつ)
    _series(conn, "USDJPY", _sine(n_in + n_out, phase=0), start=start)
    _series(conn, "EURUSD",
            _sine(n_in, phase=1) + _sine(n_out, phase=-1)[::-1],  # holdout 部を破壊
            start=start)
    r_all = lead_lag(conn, "USDJPY", "EURUSD", timeframe="1h",
                     source="dukascopy", in_sample_until=FAR_FUTURE)
    out = analyze_for_agent(conn, _settings_watch_eurusd(),
                            {"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD",
                             "timeframe": "1h"}, now=NOW)
    assert out["peak_corr"] > r_all["peak_corr"]  # holdout 汚染が除外されている


def test_agent_rejects_symbol_outside_watch_and_pairs(tmp_path):
    conn = _conn(tmp_path)
    out = analyze_for_agent(conn, SETTINGS,
                            {"kind": "lead_lag", "a": "USDJPY", "b": "GBPZAR",
                             "timeframe": "1h"}, now=NOW)
    assert out == {"error": "unknown_symbol"}  # 固定コードのみ・詳細なし


def test_error_shape_is_boundary_independent(tmp_path):
    """データ不足エラーが境界の前後・件数によらず同一応答 (サイドチャネル遮断)。"""
    conn = _conn(tmp_path)
    _series(conn, "USDJPY", _sine(3), start=H)              # 過少データ
    _series(conn, "EURUSD", _sine(3), start=H)
    out1 = analyze_for_agent(conn, _settings_watch_eurusd(),
                             {"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD",
                              "timeframe": "1h"}, now=NOW)
    _series(conn, "EURUSD", _sine(3), start=NOW - timedelta(days=10))  # holdout 期のみ追加
    out2 = analyze_for_agent(conn, _settings_watch_eurusd(),
                             {"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD",
                              "timeframe": "1h"}, now=NOW)
    assert out1 == out2 == {"error": "insufficient_data"}


def test_analysis_runs_records_trials(tmp_path):
    conn = _conn(tmp_path); _seed_two_series(conn, start=BEFORE_BOUNDARY)
    analyze_for_agent(conn, _settings_watch_eurusd(), {"kind": "corr_matrix",
                                                       "timeframe": "1h"},
                      now=NOW)
    row = conn.execute("SELECT trial_count, params_json FROM analysis_runs")\
              .fetchone()
    assert row["trial_count"] >= 1


def test_coverage_report_gap_pct(tmp_path):
    conn = _conn(tmp_path)
    _series(conn, "USDJPY", _sine(24), start=H)   # 水曜 24h — 全てオープン時間
    rep = coverage_report(conn, "USDJPY", timeframe="1h", source="dukascopy",
                          start=H, end=H + timedelta(hours=48))
    assert rep["bars"] == 24 and rep["gap_pct"] > 0  # 後半 24h が欠損


# --- 追加テスト (契約の細部・変異キラー) --------------------------------


def test_analyze_for_agent_naive_now_raises(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        analyze_for_agent(conn, SETTINGS, {"kind": "corr_matrix",
                                           "timeframe": "1h"},
                          now=datetime(2026, 8, 1))  # naive


def test_analyze_for_agent_rejects_non_dict_request(tmp_path):
    conn = _conn(tmp_path)
    assert analyze_for_agent(conn, SETTINGS, ["not", "a", "dict"],
                             now=NOW) == {"error": "invalid_request"}


def test_analyze_for_agent_rejects_unknown_kind(tmp_path):
    conn = _conn(tmp_path)
    out = analyze_for_agent(conn, SETTINGS, {"kind": "nope"}, now=NOW)
    assert out == {"error": "invalid_request"}


def test_analyze_for_agent_rejects_unhashable_kind_without_raising(tmp_path):
    """kind が非 hashable (list 等) だと `in _REQUEST_SCHEMA` の hashing で
    TypeError になり得る — request 由来の失敗は無送出契約 (§D) なので
    TypeError を漏らさず invalid_request を返す。"""
    conn = _conn(tmp_path)
    out = analyze_for_agent(
        conn, SETTINGS, {"kind": ["lead_lag"], "timeframe": "1h"}, now=NOW)
    assert out == {"error": "invalid_request"}


def test_analyze_for_agent_rejects_missing_required_key(tmp_path):
    conn = _conn(tmp_path)
    out = analyze_for_agent(conn, SETTINGS, {"kind": "corr_matrix"}, now=NOW)
    assert out == {"error": "invalid_request"}


def test_analyze_for_agent_rejects_extra_key(tmp_path):
    conn = _conn(tmp_path)
    out = analyze_for_agent(
        conn, SETTINGS,
        {"kind": "corr_matrix", "timeframe": "1h", "source": "dukascopy"},
        now=NOW)
    assert out == {"error": "invalid_request"}  # source は request から受けない


def test_analyze_for_agent_rejects_timeframe_outside_enum(tmp_path):
    conn = _conn(tmp_path)
    out = analyze_for_agent(conn, SETTINGS,
                            {"kind": "corr_matrix", "timeframe": "1s"},
                            now=NOW)
    assert out == {"error": "invalid_request"}


def test_analyze_for_agent_rejects_window_outside_enum(tmp_path):
    conn = _conn(tmp_path)
    out = analyze_for_agent(
        conn, _settings_watch_eurusd(),
        {"kind": "rolling_corr_summary", "a": "USDJPY", "b": "EURUSD",
         "timeframe": "1h", "window": 7}, now=NOW)
    assert out == {"error": "invalid_request"}


def test_analyze_for_agent_rejects_a_equals_b(tmp_path):
    conn = _conn(tmp_path)
    out = analyze_for_agent(conn, SETTINGS,
                            {"kind": "lead_lag", "a": "USDJPY", "b": "USDJPY",
                             "timeframe": "1h"}, now=NOW)
    assert out == {"error": "invalid_request"}


def test_analyze_for_agent_corr_matrix_single_candidate_is_insufficient_data(
        tmp_path):
    """既定 SETTINGS は候補が USDJPY 1 つだけ (watch_symbols=[]) — 相関を
    計算できるペアが無いので insufficient_data (0 件を分母にした
    analysis_runs.save の ValueError をそのまま漏らさない)。"""
    conn = _conn(tmp_path)
    out = analyze_for_agent(conn, SETTINGS, {"kind": "corr_matrix",
                                             "timeframe": "1h"}, now=NOW)
    assert out == {"error": "insufficient_data"}
    assert conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] \
        == 0  # 失敗時は保存しない


def test_analyze_for_agent_error_response_has_no_extra_keys(tmp_path):
    conn = _conn(tmp_path)
    out = analyze_for_agent(conn, SETTINGS, {"kind": "bogus"}, now=NOW)
    assert set(out.keys()) == {"error"}


def test_analyze_for_agent_success_saves_run_and_returns_flat_shape(tmp_path):
    conn = _conn(tmp_path)
    _seed_two_series(conn, start=BEFORE_BOUNDARY)
    out = analyze_for_agent(conn, _settings_watch_eurusd(),
                            {"kind": "rolling_corr_summary", "a": "USDJPY",
                             "b": "EURUSD", "timeframe": "1h", "window": 20},
                            now=NOW)
    assert set(out.keys()) == {"analysis_run_id", "mean", "std", "min", "max"}
    assert conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] \
        == 1


def test_analyze_for_agent_corr_matrix_pairs_keys_are_slash_joined(tmp_path):
    conn = _conn(tmp_path)
    _seed_two_series(conn, start=BEFORE_BOUNDARY)
    out = analyze_for_agent(conn, _settings_watch_eurusd(),
                            {"kind": "corr_matrix", "timeframe": "1h"},
                            now=NOW)
    assert set(out.keys()) == {"analysis_run_id", "pairs"}
    assert "USDJPY/EURUSD" in out["pairs"]
    assert isinstance(out["pairs"]["USDJPY/EURUSD"], float)


# --- 変異キラー ①: in_sample_until は排他境界でなければならない --------

def test_corr_matrix_boundary_is_exclusive_of_in_sample_until(tmp_path):
    """境界ちょうどのバーは in-sample に含めない (bar_time < in_sample_until)。
    31 本投入し、31 本目 (index 30) の bar_time を boundary にすると:
    - 排他 (仕様どおり): 30 本しか見えず、リターン 29 件 < MIN_COMMON_OBS → 不能
    - 包含 (mutant): 31 本見え、リターン 30 件 == MIN_COMMON_OBS → 計算できる
    """
    conn = _conn(tmp_path)
    start = H
    _series(conn, "USDJPY", _sine(31, phase=0), start=start)
    _series(conn, "EURUSD", _sine(31, phase=1), start=start)
    boundary = start + timedelta(hours=30)
    with pytest.raises(ValueError):
        corr_matrix(conn, ["USDJPY", "EURUSD"], timeframe="1h",
                    source="dukascopy", in_sample_until=boundary)
    ok = corr_matrix(conn, ["USDJPY", "EURUSD"], timeframe="1h",
                     source="dukascopy",
                     in_sample_until=boundary + timedelta(hours=1))
    assert ("USDJPY", "EURUSD") in ok


# --- 変異キラー ⑤: trial_count は計算した相関値の個数 (定数化させない) ---

def test_lead_lag_trial_count_equals_len_lags(tmp_path):
    conn = _conn(tmp_path)
    _seed_two_series(conn, start=BEFORE_BOUNDARY)
    analyze_for_agent(conn, _settings_watch_eurusd(),
                      {"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD",
                       "timeframe": "1h"}, now=NOW)
    row = conn.execute(
        "SELECT trial_count FROM analysis_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["trial_count"] == len(LAGS) == 25


# --- 変異キラー ⑥: リターンはギャップ (欠損バー) を跨いで作らない ------

def test_load_returns_excludes_gap_crossing_return(tmp_path):
    conn = _conn(tmp_path)
    start = H
    _series(conn, "USDJPY", _sine(10), start=start)
    gap_time = start + timedelta(hours=4)
    conn.execute("DELETE FROM ohlcv WHERE symbol='USDJPY' AND bar_time=?",
                 (gap_time.isoformat(),))
    conn.commit()
    returns = _load_returns(conn, "USDJPY", "1h", source="dukascopy",
                            in_sample_until=FAR_FUTURE)
    # gap_time 自体 (バーが無い) と gap_time+1h (直前バーが欠損) はどちらも
    # リターン未定義でなければならない — ギャップを跨いだリターンを作らない。
    assert gap_time not in returns
    assert (gap_time + timedelta(hours=1)) not in returns
    # ギャップから離れたバーは通常どおりリターンが定義される
    assert (start + timedelta(hours=2)) in returns


def test_module_enums_are_frozen_tuples():
    """パラメータは列挙制 (§6) — 型の取り違えで自由入力化しない。"""
    assert TIMEFRAMES == ("15m", "1h", "4h", "1d")
    assert WINDOWS == (20, 60, 120)
    assert list(LAGS) == list(range(-12, 13))
