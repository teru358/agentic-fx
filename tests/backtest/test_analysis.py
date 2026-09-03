"""analysis — 相関 3 種・非漏洩出力契約・analysis_runs のテスト (プラン 6 Task 10)。

brief (task-10-brief.md Step 1) のテストを、上書き節 A (コントローラ照合
2026-08-02) の読み替えを適用して収録する:

- factories の SETTINGS は pairs=["USDJPY"], datafeed.watch_symbols=[] な
  ので、EURUSD を使う analyze_for_agent 呼び出しは全て
  ``_settings_watch_eurusd()`` (ローカルヘルパ) を渡す
  (test_agent_output_contract_no_leak / test_agent_analysis_is_in_sample_bounded
  / test_analysis_runs_records_trials の 3 本)。
- ``_seed_two_series`` の既定 start=H (2026-07-22) は NOW (2026-08-01) の
  holdout boundary (2026-05-01, holdout_months=3) より後なので、
  analyze_for_agent の成功系テスト (no_leak / records_trials) では
  boundary より確実に前の start で明示的にシードする。
- round2 #9 是正 (2026-08-29、設計書 §6.1 裁定注記): `analyze_for_agent` は
  内部で `in_sample_until` (≈2026-05-01) から遡る既定 90 日窓 (`since`≈
  2026-01-31) を強制するようになった。旧 `BEFORE_BOUNDARY`
  (2026-01-01 12:00) はこの窓のわずかに外側 (=insufficient_data になる)
  だったため、窓の内側かつ boundary より確実に前の日付へ更新する
  (`_seed_two_series` は既定 200 本の 1h バー = 約 8.3 日分なので、
  start をこの日付にしても boundary を跨がない)。
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

from tests.backtest.factories import H, SETTINGS, _conn

FAR_FUTURE = datetime(2030, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)
BEFORE_BOUNDARY = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _settings_watch_eurusd():
    """factories の SETTINGS (watch_symbols=[]) を EURUSD 込みに拡張する。"""
    return SETTINGS.model_copy(update={
        "datafeed": SETTINGS.datafeed.model_copy(
            update={"watch_symbols": ["EURUSD"]})})


def test_analyze_for_agent_rejects_candidate_count_exceeding_max(tmp_path):
    """L11: `max_candidates` 超過ガード (analysis.py:398-402、実行時再検証)
    にテストが 1 本も無かった。`model_copy` は pydantic のバリデータを
    再実行しないため、`Settings.model_validate` レベルの
    `watch_symbols > max_watch_symbols` チェック (config.py:361) を経由
    せず watch_symbols を上限超過にできる — これで実行時再検証だけが
    唯一のガードになる状態を作る。"""
    conn = _conn(tmp_path)
    too_many = [f"SYM{i}" for i in range(SETTINGS.analysis.max_watch_symbols + 1)]
    settings = SETTINGS.model_copy(update={
        "datafeed": SETTINGS.datafeed.model_copy(
            update={"watch_symbols": too_many})})
    with pytest.raises(ValueError, match="candidate symbol count"):
        analyze_for_agent(conn, settings,
                          {"kind": "lead_lag", "a": "USDJPY", "b": "SYM0",
                           "timeframe": "1h"}, now=NOW)


def _series(conn, symbol, values, *, start, timeframe="1h", source="dukascopy"):
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
    ohlcv.import_history_bars(conn, rows, source=source)


def _sine(n, *, phase=0):
    """三角関数の決定的疑似価格列 (100 を中心に振幅 1)。"""
    return [100 + math.sin((i + phase) / 5.0) for i in range(n)]


def _seed_two_series(conn, *, start=H, source="dukascopy"):
    """EURUSD が USDJPY に 1 バー先行する系列 (b[t] = a[t+1] と同位相差)。"""
    _series(conn, "USDJPY", _sine(200, phase=0), start=start, source=source)
    _series(conn, "EURUSD", _sine(200, phase=1), start=start, source=source)


def test_corr_matrix_inner_join_and_gap_exclusion(tmp_path):
    conn = _conn(tmp_path); _seed_two_series(conn)
    # USDJPY 側の 1 バーを欠損させても inner join で落ちるだけで計算は通る
    conn.execute("DELETE FROM ohlcv_history WHERE symbol='USDJPY' AND bar_time=?",
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


def test_insufficient_data_error_response_does_not_leak_exception_detail(
        tmp_path, caplog):
    """裁定 D4 (2026-08-24): agent への応答は `{"error": "insufficient_data"}`
    のまま (サイドチャネル遮断は維持) だが、内部ログには例外の観測可能性を
    持たせる — 応答文字列に例外種別名やメッセージが漏れないことと、
    ログには残ることの両方を確認する。"""
    import logging
    conn_empty = _conn(tmp_path / "empty2")
    holdout_only_start = NOW - timedelta(days=10)
    _series(conn_empty, "USDJPY", _sine(20, phase=0), start=holdout_only_start)
    _series(conn_empty, "EURUSD", _sine(20, phase=1), start=holdout_only_start)
    with caplog.at_level(logging.WARNING, logger="agentic_fx.backtest.analysis"):
        out = analyze_for_agent(
            conn_empty, _settings_watch_eurusd(),
            {"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD", "timeframe": "1h"},
            now=NOW)
    assert out == {"error": "insufficient_data"}
    assert "ValueError" not in str(out) and "Traceback" not in str(out)
    assert any(r.exc_info is not None for r in caplog.records), (
        "内部ログに traceback (exc_info) が残っていない")


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
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
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
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
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
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
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


def test_coverage_report_epoch_anchor_matches_actual_for_offgrid_bounds(
        tmp_path):
    """F4 (レビュー Fix Round 1, codex Medium): ``expected_open_bars`` は
    ``bars`` (``load_resampled_frame`` の epoch 錨バケット契約) と同じ格子
    で数えなければならない。``start``/``end`` が timeframe 格子に非整列
    (オフグリッド) だと、旧実装 (``start`` から素朴に刻む) は実データと
    異なる格子を数え、データが完全に揃っていても偽の欠損率を報告し得た。

    start=H+30分 (12:30)・end=H+2h30分 (14:30)・timeframe=1h。epoch 錨の
    バケット格子で ``bucket_start>=start`` かつ ``bucket_end<=end`` を
    満たすのは [13:00,14:00) の 1 本のみ。旧実装は [12:30,14:30) を 1h
    ごとに素朴に刻んで 2 本 (12:30・13:30 起点) と数えていたため、全データ
    が揃っていても gap_pct が 50% と偽の欠損を報告していた。
    """
    conn = _conn(tmp_path)
    start = H + timedelta(minutes=30)
    end = H + timedelta(hours=2, minutes=30)
    rows = [("USDJPY", "1m", (H + timedelta(minutes=i)).isoformat(),
             100.0 + i, 100.5 + i, 99.5 + i, 100.0 + i, 1.0, 0.01)
            for i in range(150)]  # H 〜 H+2h30分、密な 1m データ (欠損なし)
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    rep = coverage_report(conn, "USDJPY", timeframe="1h", source="dukascopy",
                          start=start, end=end)
    assert rep["expected_open_bars"] == 1
    assert rep["bars"] == 1
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


# 裁定A / round2 #9 是正 (2026-08-29、設計書 §6.1 裁定注記): analyze_for_agent
# は内部で in_sample_until から遡る既定90日窓 (`since`) を強制する。
# `_REQUEST_SCHEMA` は変更していない (agent 側から窓を外す手段は無い) の
# で、窓の外側にしかデータが無い候補は insufficient_data になることを
# 直接 assert する — 「窓を外す変異で red になる pin」。
OUTSIDE_DB_READ_WINDOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_analyze_for_agent_ignores_data_outside_the_internal_db_read_window(
        tmp_path):
    """OUTSIDE_DB_READ_WINDOW (in_sample_until - 90日 の窓より前) にしか
    データが無い場合は insufficient_data になること。対照として同じ
    シェイプのデータを窓の内側 (BEFORE_BOUNDARY) に置くと成功することを
    同じテスト内で確認する — 片方だけだと `since` の算出を削る変異
    (常に None を渡す = 従来の全履歴読み) を殺せない。"""
    conn = _conn(tmp_path)
    _seed_two_series(conn, start=OUTSIDE_DB_READ_WINDOW)
    out_outside = analyze_for_agent(conn, _settings_watch_eurusd(),
                                    {"kind": "corr_matrix", "timeframe": "1h"},
                                    now=NOW)
    assert out_outside == {"error": "insufficient_data"}

    in_window_dir = tmp_path / "in_window"
    in_window_dir.mkdir()
    conn2 = _conn(in_window_dir)
    _seed_two_series(conn2, start=BEFORE_BOUNDARY)
    out_inside = analyze_for_agent(conn2, _settings_watch_eurusd(),
                                   {"kind": "corr_matrix", "timeframe": "1h"},
                                   now=NOW)
    assert "error" not in out_inside
    assert "USDJPY/EURUSD" in out_inside["pairs"]


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
    conn.execute("DELETE FROM ohlcv_history WHERE symbol='USDJPY' AND bar_time=?",
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


# --- レビュー Fix Round 1 (2026-08-02, codex + sonnet 並行レビュー) ------


def test_load_returns_offgrid_until_drops_forming_bucket_4h(tmp_path):
    """F2 (sonnet Important-1): 実運用の ``in_sample_until``
    (``holdout.in_sample_until``) は分格子にしか揃わず、4h/1d/15m のような
    粗い timeframe の格子には一般に非整合 (オフグリッド)。オフグリッド
    until では旧実装 (``bar_time < until``) と新実装
    (``bucket_end <= until``) が分岐する — 新実装は「until 時点で形成中の
    末尾バケット」を追加で落とす (意図した先読み防止の強化)。

    tf=4h・バケットにつき 1m 行 1 本 (H, H+4h, H+8h)。until=H+9h30分
    (570分、4h=240分の倍数ではないオフグリッド) では [H+8h,H+12h) は
    bucket_end=H+12h > until のため未確定 → 旧実装なら
    bar_time=H+8h < until を満たし含まれてしまっていたが、新実装は除外する。
    """
    conn = _conn(tmp_path)
    _series(conn, "USDJPY", [100.0, 101.0, 102.0], start=H, timeframe="4h")
    offgrid_until = H + timedelta(hours=9, minutes=30)  # 570分、非 4h 倍数
    returns = _load_returns(conn, "USDJPY", "4h", source="dukascopy",
                            in_sample_until=offgrid_until)
    assert (H + timedelta(hours=8)) not in returns  # 形成中バケットは不算入
    assert (H + timedelta(hours=4)) in returns
    assert returns[H + timedelta(hours=4)] == pytest.approx(math.log(101 / 100))


def test_load_returns_uses_bucket_last_close_not_first_1m_row(tmp_path):
    """F5 (codex Low, 採用): 1 バケットに 2 本以上の 1m 行がある場合の
    フォーカステスト。既存フィクスチャはバケットにつき 1m 行 1 本のため、
    「resample を素通りして 1m 行を上位足 close と誤読する」regression を
    全 analysis テストが検出できなかった。バケット内 first/last の close を
    大きく違えることで、リターンが**バケット最終 close** から計算される
    ことを直接検証する (直読みなら異なる値になる)。
    """
    conn = _conn(tmp_path)
    rows = [
        ("USDJPY", "1m", H.isoformat(), 100, 100.5, 99.5, 100, 1.0, 0.01),
        ("USDJPY", "1m", (H + timedelta(minutes=30)).isoformat(),
         105, 110.5, 99.5, 110, 1.0, 0.01),
        ("USDJPY", "1m", (H + timedelta(minutes=60)).isoformat(),
         200, 200.5, 199.5, 200, 1.0, 0.01),
        ("USDJPY", "1m", (H + timedelta(minutes=90)).isoformat(),
         210, 222.5, 199.5, 222, 1.0, 0.01),
    ]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    returns = _load_returns(conn, "USDJPY", "1h", source="dukascopy",
                            in_sample_until=H + timedelta(hours=2))
    # 正: [H,H+1h) の close はバケット最終行 (H+30分, close=110)、
    # [H+1h,H+2h) の close はバケット最終行 (H+90分, close=222)。
    # 直読み (先頭行 close=100/200 をそのまま使う) なら log(200/100) になる。
    assert returns[H + timedelta(hours=1)] == pytest.approx(math.log(222 / 110))


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


def test_analyze_for_agent_persist_false_does_not_write_analysis_runs(tmp_path):
    """Task 7-D: persist=False は analysis_runs へ書かず、保存パラメータ
    (params/trial_count/source) を返す。"""
    conn = _conn(tmp_path)
    _seed_two_series(conn, start=BEFORE_BOUNDARY)
    before = conn.execute(
        "SELECT COUNT(*) c FROM analysis_runs").fetchone()["c"]
    result = analyze_for_agent(conn, _settings_watch_eurusd(),
                               {"kind": "corr_matrix", "timeframe": "1h"},
                               now=NOW, persist=False)
    after = conn.execute(
        "SELECT COUNT(*) c FROM analysis_runs").fetchone()["c"]
    assert after == before
    assert "params" in result and "trial_count" in result and "source" in result


def test_analyze_for_agent_source_follows_backtest_settings(tmp_path):
    conn = _conn(tmp_path)
    _seed_two_series(conn, start=BEFORE_BOUNDARY, source="mt5")
    settings = _settings_watch_eurusd().model_copy(update={
        "backtest": SETTINGS.backtest.model_copy(update={"eval_source": "mt5"})})

    result = analyze_for_agent(
        conn, settings, {"kind": "corr_matrix", "timeframe": "1h"},
        now=NOW, persist=False)

    assert result["source"] == "mt5"


def test_analyze_for_agent_persist_true_keeps_existing_behavior(tmp_path):
    """既定 persist=True は従来どおり analysis_runs へ書く (回帰なし)。"""
    conn = _conn(tmp_path)
    _seed_two_series(conn, start=BEFORE_BOUNDARY)
    before = conn.execute(
        "SELECT COUNT(*) c FROM analysis_runs").fetchone()["c"]
    analyze_for_agent(conn, _settings_watch_eurusd(),
                      {"kind": "corr_matrix", "timeframe": "1h"}, now=NOW)
    after = conn.execute(
        "SELECT COUNT(*) c FROM analysis_runs").fetchone()["c"]
    assert after == before + 1


# --- round2 D1 是正 (裁定A改訂、2026-08-29): DB 読み窓の導出 -------------
#
# 検収 (acceptance-round2.md D1): 固定90日窓では `timeframe='1d'` の窓内に
# 1d バーが最大90-91本しか入らず、`rolling_corr_summary` の `window=120`
# (WINDOWS の正規の列挙値) が恒久的に insufficient_data になっていた。
# 改訂裁定は窓日数を window/timeframe から導出する (係数2、下限90日・
# 上限730日)。以下は導出関数そのものの pin (a: 導出ロジック、床・天井は
# 現行の合法な入力集合からは到達不能なので直接呼び出しで pin する) と、
# 実際に `window=120, timeframe='1d'` が成功するようになったことの pin
# (b: 退行の再発防止) の 2 段。

from agentic_fx.backtest.analysis import _compute_db_read_window_days  # noqa: E402


def test_compute_db_read_window_days_derives_bars_from_window_and_timeframe():
    """window/timeframe から必要バー数 (係数2) → 日数を導出する基本形。
    1d は 1 バー=1440分なので `required_bars * 1440 / 1440 = required_bars`
    (日数と直結する簡単なケースで導出式そのものを照合する)。"""
    assert _compute_db_read_window_days("1d", 120) == 120 * 2
    assert _compute_db_read_window_days("1d", 60) == 60 * 2
    # round2 最終是正 A1 (2026-08-29、verified-local-round2.md A1): 上の 2 本
    # は 1d のケースで `required_bars * bar_minutes / 1440` が常に整数になり
    # `bar_minutes` が約分で消えるため、`math.ceil` を floor/int/round に
    # 変えても `bar_minutes` をハードコードしても生存する。非 1d かつ
    # 非整数日になる入力で ceil と bar_minutes を同時に固定する。
    # 15m × 5000 bars: 5000*2*15/1440 = 104.166… → 切り上げ 105
    # (floor/int/round はいずれも 104 になる)。
    assert _compute_db_read_window_days("15m", 5000) == 105


def test_compute_db_read_window_days_floors_small_derivations_at_90():
    """derived が90日未満 (intraday timeframe、または window=None で
    MIN_COMMON_OBS を基準にした場合) は下限90日でクリップする — 旧既定を
    割らない安全側。現行の合法な入力集合 (TIMEFRAMES×WINDOWS、または
    window=None の corr_matrix/lead_lag) はすべてこの分岐に落ちる。"""
    assert _compute_db_read_window_days("15m", 20) == 90
    assert _compute_db_read_window_days("1h", 20) == 90
    assert _compute_db_read_window_days("1d", 20) == 90
    assert _compute_db_read_window_days("1h", None) == 90
    assert _compute_db_read_window_days("1d", None) == 90


def test_compute_db_read_window_days_caps_large_derivations_at_730():
    """derived が730日を超える場合は上限でクリップする。現行の
    TIMEFRAMES×WINDOWS の合法な組では到達しない (最大は 1d×120=240日) —
    将来 WINDOWS/TIMEFRAMES が広がっても内向き DB 走査量に上限を保つ
    多層防御なので、列挙外の値を直接渡して境界そのものを pin する
    (`min(730, ...)` を消す変異、または下限が誤って上限側を覆う変異は
    ここでしか観測できない)。"""
    assert _compute_db_read_window_days("1d", 10_000) == 730
    assert _compute_db_read_window_days("4h", 100_000) == 730


def _seed_two_series_weekdays_1d(conn, *, until, n_bars):
    """[until - n_bars 営業日, until) の直前 `n_bars` 本の**営業日のみ**の
    1d バーを USDJPY/EURUSD (EURUSD が 1 バー先行) に投入する。`until` は
    1d バケット境界 (UTC 00:00) に揃っている必要がある。

    本番の dukascopy 1d バーは週末に立たない (acceptance-round2.md D1:
    「400日連続」の合成データは窓内密度を 7/5 過大評価する) ので、D1 の
    退行 pin は営業日のみのバーで再現する — 週末ギャップは
    `_load_returns` の「ギャップを跨ぐリターンは作らない」規約により
    週明け 1 本分のリターンを毎週失わせる、より厳しい (=退行が起きやすい)
    形になる。
    """
    days = []
    d = until - timedelta(days=1)
    while len(days) < n_bars:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    a_vals = _sine(len(days), phase=0)
    b_vals = _sine(len(days), phase=1)
    a_rows = [("USDJPY", "1m", day.isoformat(), v, v + 0.05, v - 0.05, v,
               1.0, 0.01) for day, v in zip(days, a_vals)]
    b_rows = [("EURUSD", "1m", day.isoformat(), v, v + 0.05, v - 0.05, v,
               1.0, 0.01) for day, v in zip(days, b_vals)]
    ohlcv.import_history_bars(conn, a_rows + b_rows, source="dukascopy")


def test_analyze_for_agent_rolling_corr_summary_1d_window_120_succeeds(
        tmp_path):
    """D1 killer: `timeframe='1d', window=120` は WINDOWS の正規の列挙値
    であり、固定90日窓の下では ``insufficient_data`` に恒久的に固定されて
    いた (acceptance-round2.md D1)。`_compute_db_read_window_days` を旧
    実装 (`return 90` 固定) へ戻す変異、または係数/床上限の算出を壊す変異
    はいずれもこのテストを red にする。"""
    conn = _conn(tmp_path)
    in_sample_until = datetime(2026, 5, 1, tzinfo=timezone.utc)  # NOW=2026-08-01, holdout_months=3 と同じ境界
    # derived window (1d, window=120) = 240日。営業日のみで 200 本投入し
    # window+1=121 本を大きく上回らせる (週末ギャップでの目減りを吸収)。
    _seed_two_series_weekdays_1d(conn, until=in_sample_until, n_bars=200)
    out = analyze_for_agent(
        conn, _settings_watch_eurusd(),
        {"kind": "rolling_corr_summary", "a": "USDJPY", "b": "EURUSD",
         "timeframe": "1d", "window": 120}, now=NOW)
    assert "error" not in out, out
    assert set(out.keys()) == {"analysis_run_id", "mean", "std", "min", "max"}
