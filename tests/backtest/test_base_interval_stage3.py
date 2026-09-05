from datetime import timedelta

import pytest

from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.backtest.replay import ReplayClock
from agentic_fx.backtest.timeframes import ceil_to_bucket, load_resampled_frame
from tests.backtest.factories import H, _conn, _row_at
from agentic_fx.store import ohlcv


def test_5m_clock_and_ceil_use_epoch_grid():
    assert ceil_to_bucket(H + timedelta(minutes=1), "5m") == H + timedelta(minutes=5)
    clock = ReplayClock(H, timedelta(minutes=5))
    assert clock.advance() == H + timedelta(minutes=5)


def test_run_replay_rejects_start_on_minute_boundary_but_off_base_grid(tmp_path):
    """段 0 pin: start/end のグリッド検証が「分境界」まで弱体化すると、
    5m 基底で 1 分だけずれた start (分境界ではあるが 5 分格子外) を通して
    しまう — 明示的に拒否する。
    """
    from agentic_fx.backtest.runner import run_replay
    from tests.backtest.factories import SETTINGS, _conn
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        run_replay(SETTINGS, symbol="USDJPY", dataset=HistoryDataset("dukascopy", "5m"),
                   start=H + timedelta(minutes=1), end=H + timedelta(minutes=6),
                   intent_source=lambda _: None, eval_timeframe="1h",
                   history_conn=conn)


def test_native_5m_is_passthrough_and_1m_eval_is_rejected(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=5 * n), o=100 + n, h=101 + n,
                    l=99 + n, c=100 + n) for n in range(3)]
    rows = [(*row[:1], "5m", *row[2:]) for row in rows]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    frame = load_resampled_frame(conn, "USDJPY", "5m", source="dukascopy",
                                 base_interval="5m", until=H + timedelta(minutes=15))
    assert len(frame) == 3
    with pytest.raises(ValueError):
        from agentic_fx.backtest.runner import run_replay
        from tests.backtest.factories import SETTINGS
        run_replay(SETTINGS, symbol="USDJPY", dataset=HistoryDataset("dukascopy", "5m"),
                   start=H, end=H + timedelta(minutes=5), intent_source=lambda _: None,
                   eval_timeframe="1m", history_conn=conn)