"""holdout — in-sample / holdout gate 分割 (Task 9)。

上書き節 A (task-9-brief.md, コントローラ照合 2026-08-01) がこのテストの
契約: 実 run_replay は空履歴でも 30 日 136 秒かかるため、テストでは
``monkeypatch.setattr("agentic_fx.backtest.holdout.run_replay", fake)`` で
置換する。fake は受け取った kwargs (start/end/symbol/source/eval_timeframe)
を記録し、最小の BacktestResult を返す。save_harness_run/compute_metrics は
実物を使う (配線検証)。
"""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest import holdout, metrics, runner
from agentic_fx.backtest.holdout import (
    NoHistoryError, holdout_boundary, run_holdout_gate, run_in_sample,
)
from agentic_fx.backtest.runner import BacktestResult
from agentic_fx.store import ohlcv
from agentic_fx.store.backtest_runs import settings_snapshot_hash

from tests.backtest.factories import H, SETTINGS, WED, _conn, _row_at, DATASET_1M

UTC = timezone.utc

# F1/F2 (fix round 1, codex Important): 遮断 1 は例外メッセージ経路にも
# 適用される — 期間の isoformat (4 桁年 + "T" 付き時刻) が例外文字列に
# 含まれていないことを確認するヘルパ。
_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _assert_no_timestamp_leak(message: str) -> None:
    assert _ISO_TS_RE.search(message) is None, (
        f"exception message leaks an isoformat timestamp: {message!r}")


def _seed_history(hist):
    """最古バー判定に足る少数行 (fake replay なので期間を覆う必要はない)。"""
    rows = [
        _row_at(H - timedelta(days=200), o=100.0, h=100.5, l=99.5, c=100.2),
        _row_at(H - timedelta(days=199), o=100.2, h=100.6, l=99.8, c=100.3),
        _row_at(H, o=100.3, h=100.7, l=99.9, c=100.4),
    ]
    ohlcv.import_history_bars(hist, rows, source="dukascopy")


def _make_fake_replay(calls):
    def fake(settings, *, symbol, dataset, start, end, intent_source,
             eval_timeframe="1h", history_conn):
        calls.append({"symbol": symbol, "source": dataset.source, "start": start,
                      "end": end, "eval_timeframe": eval_timeframe})
        return BacktestResult(
            orders=[], equity_curve=[(start.isoformat(), 1e6)], start=start,
            end=end, source=dataset.source, fallback_spread_used=False)
    return fake


def test_holdout_boundary_simple_months():
    assert holdout_boundary(datetime(2026, 8, 1, tzinfo=UTC), 3) == \
        datetime(2026, 5, 1, tzinfo=UTC)
    assert holdout_boundary(datetime(2026, 3, 31, tzinfo=UTC), 1) == \
        datetime(2026, 2, 28, tzinfo=UTC)  # 日クランプ


def test_holdout_boundary_naive_rejected():
    with pytest.raises(ValueError):
        holdout_boundary(datetime(2026, 8, 1), 3)


def test_holdout_boundary_months_below_one_rejected():
    with pytest.raises(ValueError):
        holdout_boundary(datetime(2026, 8, 1, tzinfo=UTC), 0)


def test_no_history_error_remains_a_value_error():
    assert issubclass(NoHistoryError, ValueError)


def test_wiring_pins_real_run_replay_and_compute_metrics():
    """配線ピン: import が実物であることの固定 (上書き節 A)。"""
    assert holdout.run_replay is runner.run_replay
    assert holdout.compute_metrics is metrics.compute_metrics


def test_run_in_sample_returns_metrics_without_period(tmp_path, monkeypatch):
    """遮断 1: 期間・端点を返さない。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    calls = []
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay(calls))
    out = run_in_sample(SETTINGS, history_conn=hist, symbol="USDJPY",
                        dataset=DATASET_1M, intent_source=lambda b: None,
                        eval_timeframe="1h", plugin_ref="p", content_hash="h",
                        kind="strategy", now=WED + timedelta(days=120))
    assert "trades" in out
    forbidden = {"period_start", "period_end", "start", "end", "boundary"}
    assert forbidden.isdisjoint(out.keys())


def test_run_in_sample_oldest_bar_lookup_uses_dataset_base_interval_not_1m(
        tmp_path, monkeypatch):
    """段階 3 pin: `_oldest_bar_start` の SQL が `interval="1m"` 固定へ
    退行すると、5m dataset のみを持つ history では最古バーが見つからず
    `NoHistoryError` になる — dataset.base_interval を実際に使うことを
    直接検証する (段 0 変異「SQL の base 固定戻し」用)。
    """
    from agentic_fx.backtest.dataset import HistoryDataset
    hist = _conn(tmp_path)
    rows_5m = [
        (r[0], "5m", r[2], r[3], r[4], r[5], r[6], r[7], r[8])
        for r in [
            _row_at(H - timedelta(days=200), o=100.0, h=100.5, l=99.5, c=100.2),
            _row_at(H, o=100.3, h=100.7, l=99.9, c=100.4),
        ]
    ]
    ohlcv.import_history_bars(hist, rows_5m, source="dukascopy")
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    calls = []
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay(calls))
    saved = []
    out = run_in_sample(SETTINGS, history_conn=hist, symbol="USDJPY",
                        dataset=HistoryDataset("dukascopy", "5m"),
                        intent_source=lambda b: None,
                        eval_timeframe="1h", plugin_ref="p", content_hash="h",
                        kind="strategy", now=WED + timedelta(days=120),
                        record_fn=saved.append)
    assert "trades" in out


def test_in_sample_until_rounds_off_grid_now_to_5m_dataset_grid():
    """C4a-1: 5m dataset の境界は 1m 格子ではなく 5m 格子へ丸める。"""
    from agentic_fx.backtest.holdout import in_sample_until

    now = H + timedelta(days=120, minutes=3, seconds=30)
    assert in_sample_until(now, 3, base_interval="5m") == in_sample_until(
        H + timedelta(days=120), 3, base_interval="5m")


def test_in_sample_and_gate_use_disjoint_periods(tmp_path, monkeypatch):
    """分割点の両側が交わらないことを backtest_runs の記録で検証。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    calls = []
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay(calls))
    now = WED + timedelta(days=120)
    kw = dict(history_conn=hist, symbol="USDJPY", dataset=DATASET_1M,
              intent_source=lambda b: None, eval_timeframe="1h",
              plugin_ref="p", content_hash="h", kind="strategy", now=now)
    run_in_sample(SETTINGS, **kw)
    run_holdout_gate(SETTINGS, **kw)
    rows = {r["scope"]: dict(r) for r in hist.execute(
        "SELECT scope, period_start, period_end FROM backtest_runs")}
    assert rows["in_sample"]["period_end"] == \
        rows["holdout_gate"]["period_start"]


def test_run_in_sample_passes_oldest_bar_to_boundary_as_period(tmp_path, monkeypatch):
    """期間計算の本体検証: run_in_sample は start=最古バー・end=boundary を渡す。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    calls = []
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay(calls))
    now = WED + timedelta(days=120)
    boundary = holdout_boundary(now, SETTINGS.backtest.holdout_months)
    run_in_sample(SETTINGS, history_conn=hist, symbol="USDJPY",
                 dataset=DATASET_1M, intent_source=lambda b: None,
                 eval_timeframe="1h", plugin_ref="p", content_hash="h",
                 kind="strategy", now=now)
    assert len(calls) == 1
    assert calls[0]["start"] == H - timedelta(days=200)
    assert calls[0]["end"] == boundary
    assert calls[0]["symbol"] == "USDJPY"
    assert calls[0]["source"] == "dukascopy"
    assert calls[0]["eval_timeframe"] == "1h"


def test_run_holdout_gate_passes_boundary_to_now_as_period(tmp_path, monkeypatch):
    """期間計算の本体検証: run_holdout_gate は start=boundary・end=now を渡す。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    calls = []
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay(calls))
    now = WED + timedelta(days=120)
    boundary = holdout_boundary(now, SETTINGS.backtest.holdout_months)
    run_holdout_gate(SETTINGS, history_conn=hist, symbol="USDJPY",
                     dataset=DATASET_1M, intent_source=lambda b: None,
                     eval_timeframe="1h", plugin_ref="p", content_hash="h",
                     kind="strategy", now=now)
    assert len(calls) == 1
    assert calls[0]["start"] == boundary
    assert calls[0]["end"] == now


def test_run_in_sample_normalizes_naive_now_rejected(tmp_path, monkeypatch):
    hist = _conn(tmp_path)
    _seed_history(hist)
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay([]))
    with pytest.raises(ValueError):
        run_in_sample(SETTINGS, history_conn=hist, symbol="USDJPY",
                      dataset=DATASET_1M, intent_source=lambda b: None,
                      eval_timeframe="1h", plugin_ref="p", content_hash="h",
                      kind="strategy", now=datetime(2026, 11, 19, 12, 0))


def test_run_in_sample_no_history_rejected(tmp_path, monkeypatch):
    hist = _conn(tmp_path)  # 履歴投入なし
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay([]))
    with pytest.raises(ValueError):
        run_in_sample(SETTINGS, history_conn=hist, symbol="USDJPY",
                      dataset=DATASET_1M, intent_source=lambda b: None,
                      eval_timeframe="1h", plugin_ref="p", content_hash="h",
                      kind="strategy", now=WED + timedelta(days=120))


def test_run_in_sample_empty_period_rejected(tmp_path, monkeypatch):
    """最古バー >= boundary (in-sample 期間が空) → ValueError。境界の等号
    そのもの (最古バー == boundary ちょうど) を狙う (start > boundary への
    弱化を検出する)。"""
    hist = _conn(tmp_path)
    now = WED + timedelta(days=120)
    boundary = holdout_boundary(now, SETTINGS.backtest.holdout_months)
    rows = [_row_at(boundary, o=100.0, h=100.5, l=99.5, c=100.2)]
    ohlcv.import_history_bars(hist, rows, source="dukascopy")
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay([]))
    with pytest.raises(ValueError) as excinfo:
        run_in_sample(SETTINGS, history_conn=hist, symbol="USDJPY",
                      dataset=DATASET_1M, intent_source=lambda b: None,
                      eval_timeframe="1h", plugin_ref="p", content_hash="h",
                      kind="strategy", now=now)
    # F1 (fix round 1, codex Important): 遮断 1 (期間・端点はハーネスが
    # 所有) は例外経路にも適用される — 最古バー/boundary の isoformat が
    # メッセージに含まれていないこと。
    _assert_no_timestamp_leak(str(excinfo.value))


def test_run_in_sample_rejects_off_grid_oldest_bar(tmp_path, monkeypatch):
    """F2 (fix round 1, codex Important + sonnet Important-3): 最古バーが
    分格子外 (秒 != 0) だと import_history_bars 自体は受理してしまうが、run_in_sample
    は ReplayClock まで到達させず明示的に ValueError にする。fake replay が
    呼ばれていないこと (本番なら ReplayClock 構築時に落ちる契約違反を、
    fake 経由のテストが隠さないこと) も確認する。"""
    hist = _conn(tmp_path)
    off_grid = H.replace(second=30, microsecond=0)
    rows = [_row_at(off_grid, o=100.0, h=100.5, l=99.5, c=100.2)]
    ohlcv.import_history_bars(hist, rows, source="dukascopy")
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    calls = []
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay(calls))
    with pytest.raises(ValueError) as excinfo:
        run_in_sample(SETTINGS, history_conn=hist, symbol="USDJPY",
                      dataset=DATASET_1M, intent_source=lambda b: None,
                      eval_timeframe="1h", plugin_ref="p", content_hash="h",
                      kind="strategy", now=WED + timedelta(days=120))
    assert calls == []
    _assert_no_timestamp_leak(str(excinfo.value))


def test_run_holdout_gate_wires_eval_timeframe_and_created_at(
        tmp_path, monkeypatch):
    """F3+F4 (fix round 1, sonnet Important-1/2): eval_timeframe が
    save_harness_run の timeframe 列まで実際に配線されていること、created_at
    が UTC 正規化・分格子切り捨て済みの now であることを、"1h" 以外の
    timeframe + 非正規化 (JST・秒/マイクロ秒付き) now で直接検証する
    (全テストが "1h" 固定・既に分格子上の now だと、timeframe のハード
    コード化や now_norm→now の差し替えが偶然一致して検出できない)。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay([]))
    jst = timezone(timedelta(hours=9))
    now = datetime(2026, 11, 19, 21, 37, 42, 123456, tzinfo=jst)
    expected_created_at = datetime(2026, 11, 19, 12, 37, 0, tzinfo=UTC)
    run_holdout_gate(SETTINGS, history_conn=hist, symbol="USDJPY",
                     dataset=DATASET_1M, intent_source=lambda b: None,
                     eval_timeframe="30m", plugin_ref="p", content_hash="h",
                     kind="strategy", now=now)
    row = dict(hist.execute(
        "SELECT timeframe, created_at FROM backtest_runs").fetchone())
    assert row["timeframe"] == "30m"
    assert row["created_at"] == expected_created_at.isoformat()


def test_run_holdout_gate_saves_scope_holdout_gate(tmp_path, monkeypatch):
    hist = _conn(tmp_path)
    _seed_history(hist)
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay([]))
    now = WED + timedelta(days=120)
    out = run_holdout_gate(SETTINGS, history_conn=hist, symbol="USDJPY",
                           dataset=DATASET_1M, intent_source=lambda b: None,
                           eval_timeframe="1h", plugin_ref="p",
                           content_hash="h", kind="strategy", now=now)
    assert "trades" in out
    row = hist.execute(
        "SELECT scope, issued_by FROM backtest_runs").fetchone()
    assert row["scope"] == "holdout_gate"
    assert row["issued_by"] == "harness"


def test_holdout_boundary_normalizes_non_utc_offset():
    """UTC へ正規化してから暦月減算する (naive → UTC 見なしではなく、非 UTC
    offset も正しく変換する)。"""
    jst = timezone(timedelta(hours=9))
    # 2026-08-01 05:00+09:00 == 2026-07-31 20:00 UTC; 1 month back = June。
    # 時刻 (20:00) は変わらず、日は 6 月が 30 日までなので 31→30 にクランプ
    # される (7 月 31 日 → 6 月 30 日)。
    now = datetime(2026, 8, 1, 5, 0, tzinfo=jst)
    assert holdout_boundary(now, 1) == datetime(2026, 6, 30, 20, 0, tzinfo=UTC)


def test_run_holdout_gate_normalizes_non_utc_now_and_floors_to_minute(
        tmp_path, monkeypatch):
    """上書き節 B: now は UTC へ正規化し分格子へ切り捨ててから境界計算に
    使う (run_replay の正時格子契約)。非 UTC offset + 秒/マイクロ秒付き
    now を渡し、fake replay へ渡された kwargs が UTC・分格子であることを
    確認する。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    calls = []
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay(calls))
    jst = timezone(timedelta(hours=9))
    now = datetime(2026, 11, 19, 21, 37, 42, 123456, tzinfo=jst)
    expected_now = datetime(2026, 11, 19, 12, 37, 0, tzinfo=UTC)
    run_holdout_gate(SETTINGS, history_conn=hist, symbol="USDJPY",
                     dataset=DATASET_1M, intent_source=lambda b: None,
                     eval_timeframe="1h", plugin_ref="p", content_hash="h",
                     kind="strategy", now=now)
    assert len(calls) == 1
    assert calls[0]["end"] == expected_now
    assert calls[0]["end"].tzinfo == UTC
    assert calls[0]["start"] == holdout_boundary(
        expected_now, SETTINGS.backtest.holdout_months)


def test_save_harness_run_full_argument_wiring(tmp_path, monkeypatch):
    """§C: save_harness_run へ渡す全引数 (pair/timeframe/settings_hash/
    core_commit/initial_balance/created_at/metrics_json) を配線検証する。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay([]))
    now = WED + timedelta(days=120)
    out = run_holdout_gate(SETTINGS, history_conn=hist, symbol="USDJPY",
                           dataset=DATASET_1M, intent_source=lambda b: None,
                           eval_timeframe="1h", plugin_ref="p",
                           content_hash="h", kind="strategy", now=now)
    row = dict(hist.execute("SELECT * FROM backtest_runs").fetchone())
    assert row["pair"] == "USDJPY"
    assert row["timeframe"] == "1h"
    assert row["source"] == "dukascopy"
    assert row["plugin_ref"] == "p"
    assert row["content_hash"] == "h"
    assert row["kind"] == "strategy"
    assert row["settings_hash"] == settings_snapshot_hash(SETTINGS)
    assert row["core_commit"] == "testcommit"
    assert row["initial_balance"] == SETTINGS.backtest.initial_balance
    assert row["created_at"] == now.isoformat()
    assert json.loads(row["metrics_json"]) == out


def test_run_in_sample_record_fn_sink_does_not_write_backtest_runs(
        tmp_path, monkeypatch):
    """Task 7-D: record_fn 非 None のとき save_harness_run を呼ばず、
    sink へ保存パラメータ辞書を渡す。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay([]))
    sunk = []
    result = run_in_sample(
        SETTINGS, history_conn=hist, symbol="USDJPY", dataset=DATASET_1M,
        intent_source=lambda b: None, eval_timeframe="1h", plugin_ref="p",
        content_hash="h", kind="strategy", now=WED + timedelta(days=120),
        record_fn=sunk.append)
    count = hist.execute(
        "SELECT COUNT(*) c FROM backtest_runs").fetchone()["c"]
    assert count == 0
    assert len(sunk) == 1
    assert sunk[0]["scope"] == "in_sample"


def test_run_scope_save_kwargs_base_interval_and_params_via_record_fn(
        tmp_path, monkeypatch):
    """段階 2 レビュー是正 c2b-1: `_run_scope` が組み立てる save_kwargs の
    `base_interval` (dataset.base_interval そのもの) と `params`
    (`raw_grid_bars_expected`/`raw_grid_bars_present`) の**値**を
    record_fn 経由で直接ピンする。dataset に "5m" を使い、"1m" 固定への
    退行や params キー抜けを検知する。`run_holdout_gate` (period =
    [boundary, now]) を使い、期間端点を自分で完全制御する。
    """
    from agentic_fx.backtest.dataset import HistoryDataset
    hist = _conn(tmp_path)
    now = WED
    boundary = holdout.in_sample_until(now, SETTINGS.backtest.holdout_months)
    period_start, period_end = boundary, now
    rows_5m = [(r[0], "5m", r[2], r[3], r[4], r[5], r[6], r[7], r[8])
              for r in [_row_at(period_start + timedelta(minutes=5 * i),
                                o=100.0, h=100.5, l=99.5, c=100.2)
                       for i in range(3)]]
    ohlcv.import_history_bars(hist, rows_5m, source="dukascopy")
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay([]))
    saved = []
    run_holdout_gate(SETTINGS, history_conn=hist, symbol="USDJPY",
                     dataset=HistoryDataset("dukascopy", "5m"),
                     intent_source=lambda b: None, eval_timeframe="1h",
                     plugin_ref="p", content_hash="h", kind="strategy",
                     now=now, record_fn=saved.append)
    kwargs = saved[0]
    assert kwargs["base_interval"] == "5m"
    expected = int((period_end - period_start) // timedelta(minutes=5))
    assert kwargs["params"] == {
        "raw_grid_bars_expected": expected,
        "raw_grid_bars_present": 3}


def test_run_in_sample_without_record_fn_keeps_existing_behavior(
        tmp_path, monkeypatch):
    """既定 (record_fn なし) は従来どおり save_harness_run で永続化する。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    monkeypatch.setattr(holdout, "core_commit", lambda: "testcommit")
    monkeypatch.setattr(holdout, "run_replay", _make_fake_replay([]))
    run_in_sample(SETTINGS, history_conn=hist, symbol="USDJPY",
                  dataset=DATASET_1M, intent_source=lambda b: None,
                  eval_timeframe="1h", plugin_ref="p", content_hash="h",
                  kind="strategy", now=WED + timedelta(days=120))
    count = hist.execute(
        "SELECT COUNT(*) c FROM backtest_runs").fetchone()["c"]
    assert count == 1
