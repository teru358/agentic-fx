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
import statistics
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest.analysis import (
    LAGS, MIN_COMMON_OBS, TIMEFRAMES, WINDOWS, _load_returns, _pick_peak,
    analyze_for_agent, corr_matrix, coverage_report, lead_lag,
    rolling_corr_summary,
)
from agentic_fx.backtest.holdout import holdout_boundary
from agentic_fx.backtest.timeframes import TF_MINUTES
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
    """決定的な close 列を、timeframe 幅 (既定 1h) 刻みの **1m** バーとして
    投入する (乱数・実時刻不使用)。

    プラン 7 Task 0 で分析面が ``load_resampled_frame`` (ohlcv の 1m 行から
    読み取り時リサンプル) 経由に切り替わったため、``interval=timeframe``
    (例 "1h") の行を直接投入しても分析関数からは見えなくなった (本
    ブランチのインポータが書くのは 1m のみという前提と揃えた)。resample
    は「在る分だけ」を集約するので、timeframe バケットにつき 1m 行を
    ちょうど 1 本 (バケット境界時刻に) 置けば、旧実装 (interval=timeframe
    を直接投入) と同じ close 系列・同じ行数・同じ意味論を再現できる
    (1h 系列を 1 分刻みで敷き詰める必要はない — 例えば
    ``test_agent_analysis_is_in_sample_bounded`` は 4800 バーで 570 万行に
    なってしまう)。``start`` は timeframe の epoch 錨バケット境界に乗って
    いる必要がある。本ファイルの既存 start (H・BEFORE_BOUNDARY・NOW 由来の
    各種オフセット) は全て分=0 の時刻なので、既定の "1h" (と "4h" — 12 が
    4 の倍数の時刻はどれも成立) では整合する。**"1d" では成り立たない**
    (epoch 錨の 1d バケットは UTC 00:00 始まりなので、H=12:00Z のような
    正午始まりの行は 00:00 バケットへ丸め込まれてしまう) — 本ファイルは
    "1d" を使うテストを持たないため実害は無いが、将来 timeframe="1d" で
    ``_series`` を使う場合は ``start`` を UTC 00:00 に揃えること。
    """
    step = timedelta(minutes=TF_MINUTES[timeframe])
    rows = [(symbol, "1m", (start + i * step).isoformat(),
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


def _assert_output_contract_no_leak(out):
    """§6 出力契約: 日時・系列・観測数・端点を返さない (再帰チェック)。
    F4 (Fix Round 1, sonnet Minor-1) で 3 kind 全経路から共有するため
    ヘルパへ抽出。"""
    keys, leaves = _leaves(out)
    for leaf in leaves:
        assert not isinstance(leaf, datetime)
        assert not (isinstance(leaf, str) and len(leaf) >= 10
                    and leaf[4:5] == "-" and leaf[:4].isdigit())  # ISO 日付形
    assert "analysis_run_id" in keys
    forbidden = {"count", "n", "observations", "start", "end", "dates",
                 "period_start", "period_end", "window_series"}
    assert forbidden.isdisjoint(set(keys))


def test_agent_output_contract_no_leak(tmp_path):
    conn = _conn(tmp_path); _seed_two_series(conn, start=BEFORE_BOUNDARY)
    out = analyze_for_agent(conn, _settings_watch_eurusd(),
                            {"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD",
                             "timeframe": "1h"}, now=NOW)
    _assert_output_contract_no_leak(out)


def test_agent_output_contract_no_leak_corr_matrix(tmp_path):
    """F4 (Fix Round 1, sonnet Minor-1): corr_matrix 経路にも同じ検査を適用。"""
    conn = _conn(tmp_path); _seed_two_series(conn, start=BEFORE_BOUNDARY)
    out = analyze_for_agent(conn, _settings_watch_eurusd(),
                            {"kind": "corr_matrix", "timeframe": "1h"},
                            now=NOW)
    _assert_output_contract_no_leak(out)


def test_agent_output_contract_no_leak_rolling_corr_summary(tmp_path):
    """F4 (Fix Round 1, sonnet Minor-1): rolling_corr_summary 経路にも
    同じ検査を適用。"""
    conn = _conn(tmp_path); _seed_two_series(conn, start=BEFORE_BOUNDARY)
    out = analyze_for_agent(
        conn, _settings_watch_eurusd(),
        {"kind": "rolling_corr_summary", "a": "USDJPY", "b": "EURUSD",
         "timeframe": "1h", "window": 20}, now=NOW)
    _assert_output_contract_no_leak(out)


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


def test_error_shape_is_boundary_independent_partial_vs_empty_in_sample(
        tmp_path):
    """F1.4 (Fix Round 1, codex Minor-2): in-sample が非空だが 30 未満
    (20 本) のケースと in-sample が空 (全データが holdout 期) のケースが
    同一 {"error": "insufficient_data"} を返す (`test_error_shape_is_
    boundary_independent` の補強 — 境界依存の詳細が応答形状に漏れない)。"""
    conn_partial = _conn(tmp_path / "partial")
    _series(conn_partial, "USDJPY", _sine(20, phase=0), start=BEFORE_BOUNDARY)
    _series(conn_partial, "EURUSD", _sine(20, phase=1), start=BEFORE_BOUNDARY)
    out_partial = analyze_for_agent(
        conn_partial, _settings_watch_eurusd(),
        {"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD", "timeframe": "1h"},
        now=NOW)

    conn_empty = _conn(tmp_path / "empty")
    holdout_only_start = NOW - timedelta(days=10)  # 境界 (2026-05-01) より後
    _series(conn_empty, "USDJPY", _sine(20, phase=0), start=holdout_only_start)
    _series(conn_empty, "EURUSD", _sine(20, phase=1), start=holdout_only_start)
    out_empty = analyze_for_agent(
        conn_empty, _settings_watch_eurusd(),
        {"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD", "timeframe": "1h"},
        now=NOW)

    assert out_partial == out_empty == {"error": "insufficient_data"}


def test_agent_handles_non_positive_close_without_raising(tmp_path):
    """F1.3 (Fix Round 1, codex Important-1 = sonnet Minor-2 killer):
    close<=0 のバーが混入しても ZeroDivisionError を送出せず
    insufficient_data に写像する。

    再現の要件: 系列の**先頭バー**の close を 0 にする (先頭バーは
    ``_load_returns`` 内で「自分の prev が存在しない」ため自身のリターン
    計算はスキップされる — もし途中のバーを 0 にすると、そのバー自身の
    リターン計算 (``log(0/prev)``) が先に ValueError を送出し、次のバーの
    ``prev=0`` (ZeroDivisionError の本来の再現条件) には到達しない)。
    先頭を 0 にすることで、2 本目のバーが「prev=0 での除算」を直接踏む。
    """
    conn = _conn(tmp_path)
    values = [0.0, 100.0, 101.0, 102.0, 103.0]  # 先頭が close=0
    rows = [("USDJPY", "1m", (BEFORE_BOUNDARY + i * timedelta(hours=1))
             .isoformat(), v, v + 0.05, v - 0.05, v, 1.0, 0.01)
            for i, v in enumerate(values)]
    ohlcv.import_bars(conn, rows, source="dukascopy")
    _series(conn, "EURUSD", _sine(5, phase=1), start=BEFORE_BOUNDARY)
    out = analyze_for_agent(conn, _settings_watch_eurusd(),
                            {"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD",
                             "timeframe": "1h"}, now=NOW)
    assert out == {"error": "insufficient_data"}


def test_load_returns_excludes_non_positive_close_pairs(tmp_path):
    """F1.1 (Fix Round 1, codex Important-1 killer, 低レベル API 直接呼び
    出し): 正値ガードは ``_load_returns`` 自体が保証しなければならない
    — ``analyze_for_agent`` の外側 ``except ArithmeticError`` (F1.2) は
    あくまで防御の深層で、低レベル関数を直接呼ぶ経路 (Task 11 CLI 等、
    その except に守られない) では ``_load_returns`` 自身が
    ZeroDivisionError を送出してはならない。"""
    conn = _conn(tmp_path)
    values = [0.0, 100.0, 101.0, 102.0, 103.0]  # 先頭が close=0
    rows = [("USDJPY", "1m", (BEFORE_BOUNDARY + i * timedelta(hours=1))
             .isoformat(), v, v + 0.05, v - 0.05, v, 1.0, 0.01)
            for i, v in enumerate(values)]
    ohlcv.import_bars(conn, rows, source="dukascopy")
    returns = _load_returns(conn, "USDJPY", "1h", source="dukascopy",
                            in_sample_until=FAR_FUTURE)  # ここで例外なし
    # 2 本目 (close=100.0, prev=close=0.0) は正値ガードにより除外されな
    # ければならない (さもなくば 100.0/0.0 で ZeroDivisionError)。
    second_bar_time = BEFORE_BOUNDARY + timedelta(hours=1)
    assert second_bar_time not in returns


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


def test_coverage_report_allows_1m(tmp_path):
    """F3 (最終レビュー opus I-2 must-fix): coverage_report は 1m を拒否
    しない — 本ブランチのインポータが書く唯一のデータを人間が検査できる
    こと (TIMEFRAMES/_TF_MINUTES は不変のまま、coverage_report だけが
    1m を追加で受け付ける)。``_series`` は 1h 固定間隔なので使わず、直接
    1 分刻みの行を投入する。"""
    conn = _conn(tmp_path)
    values = _sine(60)
    rows = [("USDJPY", "1m", (H + timedelta(minutes=i)).isoformat(),
             v, v + 0.05, v - 0.05, v, 1.0, 0.01)
            for i, v in enumerate(values)]
    ohlcv.import_bars(conn, rows, source="dukascopy")
    rep = coverage_report(conn, "USDJPY", timeframe="1m", source="dukascopy",
                          start=H, end=H + timedelta(minutes=60))
    # H は水曜 12:00 UTC (全てオープン時間) なので、1 分刻みのステップが
    # 実際に使われていれば expected_open_bars == 60・gap_pct == 0 になる。
    # bars (COUNT(*)) だけの確認では `_COVERAGE_TF_MINUTES["1m"]` を 1 以外
    # (例: 60) に変異させても検出できない (bars は timeframe に依存しない) —
    # expected_open_bars/gap_pct まで見て「1 分ステップが実際に使われた」
    # ことを直接検証する。
    assert rep["bars"] == 60
    assert rep["expected_open_bars"] == 60
    assert rep["gap_pct"] == 0.0


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


# --- Fix Round 1 (sonnet + codex 統合裁定, F1-F6) ------------------------


def test_rolling_corr_summary_matches_manual_stdev_not_population_stdev(
        tmp_path):
    """F2 (Fix Round 1, sonnet 自己変異 M-S1 killer): std は標本標準偏差
    (statistics.stdev, N-1 分母) — 母標準偏差 (pstdev, N 分母) と区別できる
    精度で照合する。テスト側で独立に log リターン・窓相関を再計算し、
    rolling_corr_summary の出力と厳密照合する。"""
    conn = _conn(tmp_path)
    n = 31  # 30 リターン == MIN_COMMON_OBS ちょうど、window=20 → 11 窓
    a_vals = [100 + math.sin(i / 3.0) for i in range(n)]
    b_vals = [100 + math.sin(i / 7.0 + 1.3) for i in range(n)]
    _series(conn, "USDJPY", a_vals, start=BEFORE_BOUNDARY)
    _series(conn, "EURUSD", b_vals, start=BEFORE_BOUNDARY)

    # production と同じ log リターン定義 (連続バー・ギャップ無しなので
    # 単純な逐次差分)。
    ret_a = [math.log(a_vals[i] / a_vals[i - 1]) for i in range(1, n)]
    ret_b = [math.log(b_vals[i] / b_vals[i - 1]) for i in range(1, n)]
    window = 20
    n_windows = len(ret_a) - window + 1
    corrs = [statistics.correlation(ret_a[i:i + window], ret_b[i:i + window])
            for i in range(n_windows)]
    expected_std = statistics.stdev(corrs)
    expected_pstdev = statistics.pstdev(corrs)
    # この系列で stdev と pstdev が判別可能なほど異なることを確認
    # (さもないと以下の照合が stdev/pstdev の取り違えを検出できない)。
    assert expected_std != pytest.approx(expected_pstdev, rel=1e-6)

    out = rolling_corr_summary(conn, "USDJPY", "EURUSD", timeframe="1h",
                               window=window, source="dukascopy",
                               in_sample_until=FAR_FUTURE)
    assert out["mean"] == pytest.approx(statistics.mean(corrs))
    assert out["std"] == pytest.approx(expected_std, rel=1e-9)
    assert out["std"] != pytest.approx(expected_pstdev, rel=1e-6)
    assert out["min"] == pytest.approx(min(corrs))
    assert out["max"] == pytest.approx(max(corrs))


def test_pick_peak_prefers_larger_corr():
    """F3 (Fix Round 1, sonnet 自己変異 M-S2 killer): 抽出した _pick_peak
    の基本挙動 (単純な最大値選択)。"""
    assert _pick_peak({-3: 0.1, 0: 0.2, 3: 0.5}) == 3


def test_pick_peak_tie_prefers_smaller_abs_k():
    assert _pick_peak({5: 0.5, 2: 0.5, -2: 0.3}) == 2


def test_pick_peak_tie_break_prefers_ascending_k_on_abs_tie():
    """|k| が同点 (3 と -3) の場合は k 昇順 (より小さい値、= 負の方) を
    選ぶ。変異 `(corrs[k], 0, 0)` (tie-break 除去) は dict 走査順で最初に
    現れた最大キーをそのまま返す (Python の max は同点で最初の要素を保持)
    ため、挿入順を k=3 → k=-3 にしておけば、この tie を検出できる。"""
    assert _pick_peak({3: 0.5, -3: 0.5, 0: 0.2}) == -3


# --- F1 (最終レビュー opus I-1 must-fix): 境界算術の一元化 --------------

def test_analyze_for_agent_boundary_matches_between_seconds_and_minute_grid_now(
        tmp_path):
    """秒/マイクロ秒付き now と分格子 now が同一の in-sample 境界 (= 同一の
    分析結果) を返す。境界ちょうどのバーを 31 本目に置き、境界が分格子へ
    正しく切り捨てられていれば (30 本 = 29 リターン < MIN_COMMON_OBS) は
    insufficient_data、切り捨てが漏れて秒/µs 分だけ境界が後ろへずれると
    (31 本 = 30 リターン == MIN_COMMON_OBS) 計算できてしまう — この差で
    境界ちょうどのバーの混入を直接検出する。"""
    conn = _conn(tmp_path)
    boundary = holdout_boundary(NOW, SETTINGS.backtest.holdout_months)
    start = boundary - timedelta(hours=30)
    _series(conn, "USDJPY", _sine(31, phase=0), start=start)
    _series(conn, "EURUSD", _sine(31, phase=1), start=start)
    now_with_seconds = NOW + timedelta(seconds=42, microseconds=123456)
    request = {"kind": "corr_matrix", "timeframe": "1h"}
    out_seconds = analyze_for_agent(conn, _settings_watch_eurusd(), request,
                                    now=now_with_seconds)
    out_floor = analyze_for_agent(conn, _settings_watch_eurusd(), request,
                                  now=NOW)
    assert out_seconds == out_floor == {"error": "insufficient_data"}


def test_corr_matrix_perfect_positive_and_negative_correlation(tmp_path):
    """F5 (Fix Round 1, codex Minor-1): 解析的に正解が既知の系列で値その
    ものを検算する。同一系列 → corr == 1.0。log リターンが厳密に符号反転
    する系列 (b = C / a、log(b[i]/b[i-1]) = -log(a[i]/a[i-1])) → corr ==
    -1.0 (単純な価格反転 (200 - a) では log リターンは厳密には符号反転
    しないため、逆数系列を使う)。"""
    conn = _conn(tmp_path)
    n = 40
    a_vals = [100 + math.sin(i / 4.0) for i in range(n)]  # 非定数・正値
    b_same = list(a_vals)
    b_recip = [10000.0 / v for v in a_vals]
    _series(conn, "AAA", a_vals, start=BEFORE_BOUNDARY)
    _series(conn, "BBB", b_same, start=BEFORE_BOUNDARY)
    _series(conn, "CCC", b_recip, start=BEFORE_BOUNDARY)
    m = corr_matrix(conn, ["AAA", "BBB", "CCC"], timeframe="1h",
                    source="dukascopy", in_sample_until=FAR_FUTURE)
    assert m[("AAA", "BBB")] == pytest.approx(1.0, abs=1e-9)
    assert m[("AAA", "CCC")] == pytest.approx(-1.0, abs=1e-9)
