"""同期 test の共通部品: 設定・偽の bridge・既存履歴の播種。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from agentic_fx.config import load_settings
from agentic_fx.core.market_hours import is_market_open
from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect, init_db

UTC = timezone.utc
# 2026-11-11 は水曜。冬時間 (閉場は金曜 22:00Z、日曜 22:00Z に開場)。
T = datetime(2026, 11, 11, 12, 0, 7, tzinfo=UTC)
_EXAMPLE = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
WIDTH = timedelta(minutes=15)
SYMBOL = "USDJPY"
REF = "JP225"


def make_settings(*, holdout_months=1, interval="15m", lookback_days=2, watch=(),
                  eval_source="mt5", bridge_url="http://bridge.invalid"):
    mt5 = _EXAMPLE.datafeed.mt5.model_copy(update={"bridge_url": bridge_url})
    datafeed = _EXAMPLE.datafeed.model_copy(update={"mt5": mt5, "watch_symbols": list(watch)})
    sync = _EXAMPLE.backtest.history_sync.model_copy(update={"lookback_days": lookback_days})
    backtest = _EXAMPLE.backtest.model_copy(update={
        "eval_source": eval_source, "base_interval": interval,
        "holdout_months": holdout_months, "history_sync": sync})
    return _EXAMPLE.model_copy(update={"backtest": backtest, "datafeed": datafeed,
                                       "pairs": [SYMBOL]})


def conn_at(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def values(symbol, t, *, shift=0.0):
    index = int((t - datetime(1970, 1, 1, tzinfo=UTC)) // WIDTH)
    base = (100.0 if symbol == SYMBOL else 30000.0) + (index % 7) * 0.01 + shift
    return base, base + 0.05, base - 0.05, base + 0.02


def grid(start, end):
    t = start
    while t < end:
        yield t
        t += WIDTH


def seed(conn, symbol, start, end, *, skip=lambda t: False, shift=0.0):
    rows = []
    for t in grid(start, end):
        if not is_market_open(t) or skip(t):
            continue
        o, h, low, c = values(symbol, t, shift=shift)
        rows.append((symbol, "15m", t.isoformat(), o, h, low, c, 10.0, None))
    ohlcv.import_history_bars(conn, rows, source="mt5")
    return len(rows)


def count(conn, symbol=None, table="ohlcv_history"):
    if symbol is None:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE symbol=?", (symbol,)).fetchone()[0]


class FakeBridge:
    """URL の窓を解釈して 15m 足を返す。`shift` / `drop` / `missing_days` で応答を歪める。"""

    def __init__(self, *, shift=0.0, drop=lambda symbol, t: False, change=None, fail=None):
        self.calls: list[tuple[str, datetime, datetime]] = []
        self.shift = shift
        self.drop = drop
        self.change = change or {}
        self.fail = fail

    def __call__(self, url):
        parsed = urlparse(url)
        symbol = parsed.path.rsplit("/", 1)[1]
        query = parse_qs(parsed.query)
        start = datetime.fromisoformat(unquote(query["from"][0]))
        end = datetime.fromisoformat(unquote(query["to"][0]))
        self.calls.append((symbol, start, end))
        if self.fail is not None:
            raise self.fail
        bars = []
        t = start
        while t <= end:
            if is_market_open(t) and not self.drop(symbol, t):
                o, h, low, c = values(symbol, t, shift=self.shift)
                c += self.change.get((symbol, t), 0.0)
                h = max(h, c)
                bars.append({"time": t.isoformat(), "open": o, "high": h, "low": low,
                             "close": c, "volume": 10.0})
            t += WIDTH
        return {"symbol": symbol, "interval": "15m", "bars": bars}


class Clock:
    """要求の間隔を待つ代わりに仮想時計を進める。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
