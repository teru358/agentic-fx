import lzma
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.config import load_settings
from agentic_fx.store.db import connect, init_db

H = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
WED = H  # 2026-07-22 は水曜 — 市場オープン
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
DATASET_1M = HistoryDataset("dukascopy", "1m")


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _row_at(ts, *, o, h, l, c, v=10.0, spread=0.01, symbol="USDJPY"):
    """ohlcv.import_history_bars 用の row タプル。"""
    return (symbol, "1m", ts.isoformat(), o, h, l, c, v, spread)


def _bi5(records):
    """(ms, ask_points, bid_points, ask_vol, bid_vol) 列 → bi5 バイト列。"""
    raw = b"".join(struct.pack(">3i2f", *r) for r in records)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)
