# Phase 1 プラン 3: データ層 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 価格 (yfinance デフォルト + 健全性検証 + フォールバック連鎖)、テクニカル指標 + MTF、ニュース収集 (feed/web fetcher + ChromaDB RAG)、経済指標カレンダーを構築し、`datafeed/store` の API を pytest から直接検証可能にする。

**Architecture:** `src/agentic_fx/datafeed/` にデータ収集、`store/rag.py` に ChromaDB。プラン 2 の注入点 (`quote_fn` / `spec_fn` / `bars_fn` / `on_news_cycle`) に差し込める実装を提供する。内部マイルストーン: **3A** = 価格 + 指標 (クリティカルパス) / **3B** = ニュース + RAG / **3C** = econ カレンダー + init 拡張。

**Tech Stack:** yfinance / pandas / httpx / feedparser / trafilatura / chromadb / pytest (外部アクセスは全テストでモック)

## Global Constraints (設計書より)

- **デフォルトはすべて yfinance** (キー不要)。MT5 / Twelve Data は settings.yaml の追加設定。優先順位 MT5 → TD → yfinance (設計書 §5)
- **フォールバック採用条件は「取得成功」でなくデータ健全性検証の通過** (鮮度・連続性・異常値)。全ソース不健全なら `DataUnhealthy` を上げ、呼び出し側 (プラン 5) が fail closed (設計書 §5)
- yfinance のみで得たデータには**データ品質フラグ** (`source` フィールド) を残す (設計書 §5)
- news fetcher は **feed / web の 2 組み込みのみ** (plugin ではない、feedly なし) (設計書 §6)
- ニュース RAG は **48h で掃除** (設計書 §12)
- 素の clone でも動くよう**基本ニュースソース数件の初期データ**を `src/` 側にコミット (設計書 §6)
- 外部 API を叩くテストは書かない — fetch 層は全部モック。パース・検証・キャッシュロジックをテストする
- `config/settings.yaml.example` 変更時は Settings モデルと同期
- コミットメッセージ末尾: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

**プラン 1/2 から消費する契約** (変更禁止): `Quote / Bar / InstrumentSpec / Clock`、`store/ohlcv.upsert_bars / load_bars`、`store/news_sources`、`store/econ_events`、`Settings.datafeed (yfinance/mt5/twelvedata/freshness_max_min)`、`ActivityLog / Category`

---

### Task 1: 依存追加 + データ健全性検証 (datafeed/health.py)

**Files:**
- Create: `src/agentic_fx/datafeed/__init__.py`, `src/agentic_fx/datafeed/health.py`
- Test: `tests/datafeed/__init__.py`, `tests/datafeed/test_health.py`

**Interfaces:**
- Produces:
  - `DataUnhealthy(Exception)` — メッセージに理由を含む
  - `validate_quote(quote: Quote, now: datetime, freshness_max_min: float) -> None` — 鮮度超過・非正値・bid>ask で `DataUnhealthy`
  - `validate_bars(bars: list[Bar], now: datetime, freshness_max_min: float, interval_min: float) -> None` — 空・最終バー鮮度・連続性 (欠損 3 本以上)・異常値 (0/NaN/前バー比 ±10% スパイク) で `DataUnhealthy`

- [ ] **Step 1: 依存を追加**

```bash
uv add yfinance pandas httpx feedparser trafilatura chromadb
```

- [ ] **Step 2: 失敗するテストを書く**

`tests/datafeed/test_health.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.contracts import Bar, Quote
from agentic_fx.datafeed.health import (
    DataUnhealthy, validate_bars, validate_quote,
)

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _bars(n=30, start=None, step_min=60, base=148.0):
    start = start or (NOW - timedelta(minutes=step_min * n))
    return [Bar("USDJPY", "1h", start + timedelta(minutes=step_min * i),
                base, base + 0.1, base - 0.1, base + 0.05, 100)
            for i in range(n)]


def test_fresh_quote_ok():
    validate_quote(Quote("USDJPY", 148.49, 148.51, NOW, "yfinance"),
                   NOW, freshness_max_min=20)


def test_stale_quote_rejected():
    q = Quote("USDJPY", 148.49, 148.51, NOW - timedelta(minutes=25), "yfinance")
    with pytest.raises(DataUnhealthy, match="stale"):
        validate_quote(q, NOW, freshness_max_min=20)


def test_inverted_quote_rejected():
    q = Quote("USDJPY", 148.52, 148.51, NOW, "yfinance")
    with pytest.raises(DataUnhealthy, match="bid/ask"):
        validate_quote(q, NOW, freshness_max_min=20)


def test_bars_ok():
    validate_bars(_bars(), NOW, freshness_max_min=90, interval_min=60)


def test_empty_bars_rejected():
    with pytest.raises(DataUnhealthy, match="empty"):
        validate_bars([], NOW, freshness_max_min=90, interval_min=60)


def test_stale_last_bar_rejected():
    old = _bars(n=10, start=NOW - timedelta(hours=20))
    with pytest.raises(DataUnhealthy, match="stale"):
        validate_bars(old, NOW, freshness_max_min=90, interval_min=60)


def test_gap_rejected():
    bars = _bars()
    del bars[10:14]  # 4 本欠損
    with pytest.raises(DataUnhealthy, match="gap"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)


def test_spike_rejected():
    bars = _bars()
    bad = bars[15]
    bars[15] = Bar(bad.symbol, bad.interval, bad.ts, bad.open,
                   bad.high, bad.low, bad.close * 1.2, bad.volume)  # +20%
    with pytest.raises(DataUnhealthy, match="anomal"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)


def test_zero_price_rejected():
    bars = _bars()
    bad = bars[5]
    bars[5] = Bar(bad.symbol, bad.interval, bad.ts, 0.0, bad.high,
                  bad.low, bad.close, bad.volume)
    with pytest.raises(DataUnhealthy, match="anomal"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)
```

- [ ] **Step 3: テストが失敗することを確認**

Run: `uv run pytest tests/datafeed/test_health.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 4: 実装**

`src/agentic_fx/datafeed/health.py`:

```python
"""データ健全性検証 — 「取得成功」でなくこの検証の通過がフォールバック採用条件 (設計書 §5)。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from agentic_fx.core.contracts import Bar, Quote

_MAX_GAP_BARS = 3
_SPIKE_PCT = 10.0


class DataUnhealthy(Exception):
    pass


def validate_quote(quote: Quote, now: datetime,
                   freshness_max_min: float) -> None:
    if now - quote.ts > timedelta(minutes=freshness_max_min):
        raise DataUnhealthy(f"quote stale: {quote.ts} (source={quote.source})")
    if not (quote.bid > 0 and quote.ask > 0 and
            math.isfinite(quote.bid) and math.isfinite(quote.ask)):
        raise DataUnhealthy("quote has non-positive/NaN price")
    if quote.bid > quote.ask:
        raise DataUnhealthy(f"bid/ask inverted: {quote.bid} > {quote.ask}")


def validate_bars(bars: list[Bar], now: datetime, freshness_max_min: float,
                  interval_min: float) -> None:
    if not bars:
        raise DataUnhealthy("empty bars")
    if now - bars[-1].ts > timedelta(minutes=freshness_max_min):
        raise DataUnhealthy(f"bars stale: last={bars[-1].ts}")
    prev = None
    for b in bars:
        vals = (b.open, b.high, b.low, b.close)
        if any(v <= 0 or not math.isfinite(v) for v in vals):
            raise DataUnhealthy(f"anomalous bar (zero/NaN) at {b.ts}")
        if prev is not None:
            gap = (b.ts - prev.ts).total_seconds() / 60 / interval_min
            if gap > _MAX_GAP_BARS:
                raise DataUnhealthy(f"gap of {gap:.0f} bars before {b.ts}")
            move = abs(b.close - prev.close) / prev.close * 100
            if move > _SPIKE_PCT:
                raise DataUnhealthy(f"anomalous spike {move:.1f}% at {b.ts}")
        prev = b
```

- [ ] **Step 5: テストが通ることを確認**

Run: `uv run pytest tests/datafeed/test_health.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/agentic_fx/datafeed tests/datafeed
git commit -m "feat: データ健全性検証 (鮮度・連続性・異常値) + datafeed 依存"
```

---

### Task 2: ソース fetcher (datafeed/sources.py)

外部 API ごとの薄い取得層。**すべてモックでテスト** (整形ロジックを検証)。

**Files:**
- Create: `src/agentic_fx/datafeed/sources.py`
- Test: `tests/datafeed/test_sources.py`

**Interfaces:**
- Produces (各関数はネットワーク例外を握らず送出する — 選択は price_provider の責務):
  - `yf_quote(pair: str) -> Quote` / `yf_bars(pair: str, interval: str, lookback_days: int) -> list[Bar]` — yfinance。pair `USDJPY` → ticker `USDJPY=X`。quote は直近 1m バーの close を bid=ask=mid として返す (yfinance に板がないため。source="yfinance")
  - `mt5_quote(bridge_url: str, pair: str) -> Quote` / `mt5_bars(bridge_url, pair, interval, lookback_days) -> list[Bar]` — httpx GET `{bridge_url}/quote?symbol=` / `/rates?symbol=&timeframe=&count=` (mt5_bridge の既存エンドポイント形式)。source="mt5"
  - `td_bars(api_key: str, pair: str, interval: str, lookback_days: int) -> list[Bar]` — Twelve Data `/time_series`。source="twelvedata"
  - `INTERVAL_MIN: dict[str, float]` = `{"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}`

- [ ] **Step 1: 失敗するテストを書く**

`tests/datafeed/test_sources.py`:

```python
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from agentic_fx.datafeed.sources import (
    INTERVAL_MIN, mt5_quote, td_bars, yf_bars, yf_quote,
)

IDX = pd.DatetimeIndex(
    [datetime(2026, 7, 22, 11, 58, tzinfo=timezone.utc),
     datetime(2026, 7, 22, 11, 59, tzinfo=timezone.utc)])
DF = pd.DataFrame({"Open": [148.4, 148.45], "High": [148.5, 148.55],
                   "Low": [148.3, 148.4], "Close": [148.45, 148.5],
                   "Volume": [100, 120]}, index=IDX)


def test_yf_bars_maps_dataframe():
    with patch("yfinance.download", return_value=DF) as dl:
        bars = yf_bars("USDJPY", "1m", 1)
    assert dl.call_args[0][0] == "USDJPY=X"
    assert len(bars) == 2
    assert bars[-1].close == 148.5
    assert bars[-1].source if hasattr(bars[-1], "source") else True


def test_yf_quote_uses_last_close():
    with patch("yfinance.download", return_value=DF):
        q = yf_quote("USDJPY")
    assert q.bid == q.ask == 148.5
    assert q.source == "yfinance"


def test_mt5_quote_parses_bridge_response():
    resp = MagicMock()
    resp.json.return_value = {"bid": 148.49, "ask": 148.51,
                              "time": "2026-07-22T11:59:30+00:00"}
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp) as g:
        q = mt5_quote("http://localhost:8812", "USDJPY")
    assert "quote" in g.call_args[0][0]
    assert q.bid == 148.49 and q.source == "mt5"


def test_td_bars_parses_values():
    resp = MagicMock()
    resp.json.return_value = {"values": [
        {"datetime": "2026-07-22 11:59:00", "open": "148.45",
         "high": "148.55", "low": "148.40", "close": "148.50"},
        {"datetime": "2026-07-22 11:58:00", "open": "148.40",
         "high": "148.50", "low": "148.30", "close": "148.45"},
    ]}
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp):
        bars = td_bars("key", "USDJPY", "1m", 1)
    assert bars[0].ts < bars[1].ts  # 昇順に並べ替え
    assert bars[-1].close == 148.5
    assert bars[-1].source == "twelvedata" if hasattr(bars[-1], "source") \
        else True


def test_interval_table():
    assert INTERVAL_MIN["1h"] == 60
```

注: `Bar` は `source` フィールドを持たない (プラン 1 契約)。ソース識別は price_provider が返す `PriceResult.source` で扱う (Task 3)。上のテストの hasattr 分岐はこのための緩衝で、実装後に不要なら単純化してよい。

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/datafeed/test_sources.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/datafeed/sources.py`:

```python
"""ソース別 fetcher (yfinance / MT5 bridge / Twelve Data)。例外は送出、選択は provider。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import yfinance

from agentic_fx.core.contracts import Bar, Quote

INTERVAL_MIN: dict[str, float] = {
    "1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}

_YF_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h",
                "4h": "1h", "1d": "1d"}  # 4h は 1h から MTF リサンプル


def _yf_ticker(pair: str) -> str:
    return f"{pair}=X"


def yf_bars(pair: str, interval: str, lookback_days: int) -> list[Bar]:
    df = yfinance.download(
        _yf_ticker(pair), interval=_YF_INTERVAL[interval],
        period=f"{lookback_days}d", progress=False, auto_adjust=False)
    bars: list[Bar] = []
    for ts, row in df.iterrows():
        t = ts.to_pydatetime()
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        bars.append(Bar(pair, interval, t, float(row["Open"]),
                        float(row["High"]), float(row["Low"]),
                        float(row["Close"]),
                        float(row.get("Volume", 0) or 0)))
    return bars


def yf_quote(pair: str) -> Quote:
    bars = yf_bars(pair, "1m", 1)
    if not bars:
        raise RuntimeError(f"yfinance returned no data for {pair}")
    last = bars[-1]
    return Quote(pair, last.close, last.close, last.ts, "yfinance")


def mt5_quote(bridge_url: str, pair: str) -> Quote:
    r = httpx.get(f"{bridge_url}/quote", params={"symbol": pair}, timeout=10)
    r.raise_for_status()
    d = r.json()
    return Quote(pair, float(d["bid"]), float(d["ask"]),
                 datetime.fromisoformat(d["time"]), "mt5")


def mt5_bars(bridge_url: str, pair: str, interval: str,
             lookback_days: int) -> list[Bar]:
    count = int(lookback_days * 1440 / INTERVAL_MIN[interval])
    r = httpx.get(f"{bridge_url}/rates",
                  params={"symbol": pair, "timeframe": interval,
                          "count": count}, timeout=30)
    r.raise_for_status()
    return [Bar(pair, interval, datetime.fromisoformat(x["time"]),
                float(x["open"]), float(x["high"]), float(x["low"]),
                float(x["close"]), float(x.get("tick_volume", 0)))
            for x in r.json()]


def td_bars(api_key: str, pair: str, interval: str,
            lookback_days: int) -> list[Bar]:
    symbol = f"{pair[:3]}/{pair[3:]}"
    r = httpx.get("https://api.twelvedata.com/time_series",
                  params={"symbol": symbol, "interval": interval,
                          "outputsize": int(lookback_days * 1440
                                            / INTERVAL_MIN[interval]),
                          "apikey": api_key}, timeout=30)
    r.raise_for_status()
    values = r.json().get("values", [])
    bars = [Bar(pair, interval,
                datetime.fromisoformat(v["datetime"]).replace(
                    tzinfo=timezone.utc),
                float(v["open"]), float(v["high"]), float(v["low"]),
                float(v["close"]), 0.0)
            for v in values]
    bars.sort(key=lambda b: b.ts)
    return bars
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/datafeed/test_sources.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/datafeed/sources.py tests/datafeed/test_sources.py
git commit -m "feat: ソース fetcher (yfinance/MT5 bridge/Twelve Data、全モックテスト)"
```

---

### Task 3: PriceProvider (datafeed/price_provider.py)

**Files:**
- Create: `src/agentic_fx/datafeed/price_provider.py`
- Test: `tests/datafeed/test_price_provider.py`

**Interfaces:**
- Produces: `class PriceProvider`:
  - `__init__(self, conn, settings: Settings, clock: Clock)`
  - `get_quote(self, pair: str) -> Quote` — 優先順位 **MT5 → TD (quote なし、スキップ) → yfinance**。enabled なソースを順に試し、**取得成功 + `validate_quote` 通過**した最初の Quote を返す。全滅なら `DataUnhealthy`
  - `get_bars(self, pair: str, interval: str, lookback_days: int = 5) -> list[Bar]` — 同様に MT5 → TD → yfinance。健全性通過後 **ohlcv キャッシュに upsert** して返す。全滅時、キャッシュに健全なバーがあればそれを返し (source は "cache")、なければ `DataUnhealthy`
  - `latest_1m_bar(self, pair: str) -> Bar | None` — scheduler の `bars_fn` 用。`get_bars(pair, "1m", 1)` の最終バー、`DataUnhealthy` は None (fills 判定をスキップさせる)
  - `spec(self, pair: str) -> InstrumentSpec` — executor の `spec_fn` 用。Phase 1 は組み込みテーブル (USDJPY/EURUSD: pip_size 0.01/0.0001, min 0.01, max 50, step 0.01, contract 100,000)。Phase 3 で MT5 照会に置換
  - `healthcheck(self, pair: str) -> str` — quote が取れた source 名を返す (trade_loop の fail-closed 判定 + init 接続確認用)。全滅は `DataUnhealthy`
- 各ソースの失敗 (例外 / 検証不合格) は技術ログ warning + 次ソースへ。採用 source が yfinance の場合は品質フラグとしてそのまま `Quote.source` / activity に残る

- [ ] **Step 1: 失敗するテストを書く**

`tests/datafeed/test_price_provider.py`:

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar, FixedClock, Quote
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.store.db import connect, init_db
from agentic_fx.store import ohlcv

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"


def _provider(tmp_path, mt5=False):
    s = load_settings(EXAMPLE)
    if mt5:
        s = s.model_copy(deep=True)
        s.datafeed.mt5.enabled = True
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn, PriceProvider(conn, s, FixedClock(NOW))


def _fresh_bars(pair="USDJPY", n=30):
    start = NOW - timedelta(minutes=n)
    return [Bar(pair, "1m", start + timedelta(minutes=i),
                148.0, 148.1, 147.9, 148.05, 10) for i in range(n)]


def test_default_uses_yfinance(tmp_path):
    _, p = _provider(tmp_path)
    q = Quote("USDJPY", 148.5, 148.5, NOW, "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=q):
        assert p.get_quote("USDJPY").source == "yfinance"


def test_mt5_preferred_when_enabled(tmp_path):
    _, p = _provider(tmp_path, mt5=True)
    mq = Quote("USDJPY", 148.49, 148.51, NOW, "mt5")
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_quote",
               return_value=mq) as m:
        q = p.get_quote("USDJPY")
    assert q.source == "mt5"
    m.assert_called_once()


def test_falls_back_on_mt5_failure(tmp_path):
    _, p = _provider(tmp_path, mt5=True)
    yq = Quote("USDJPY", 148.5, 148.5, NOW, "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_quote",
               side_effect=OSError("bridge down")), \
         patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=yq):
        assert p.get_quote("USDJPY").source == "yfinance"


def test_unhealthy_quote_falls_through(tmp_path):
    _, p = _provider(tmp_path, mt5=True)
    stale = Quote("USDJPY", 148.49, 148.51, NOW - timedelta(hours=2), "mt5")
    yq = Quote("USDJPY", 148.5, 148.5, NOW, "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_quote",
               return_value=stale), \
         patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=yq):
        assert p.get_quote("USDJPY").source == "yfinance"


def test_all_sources_dead_raises(tmp_path):
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               side_effect=OSError("down")):
        with pytest.raises(DataUnhealthy):
            p.get_quote("USDJPY")


def test_get_bars_caches(tmp_path):
    conn, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars()):
        bars = p.get_bars("USDJPY", "1m", 1)
    assert len(bars) == 30
    assert len(ohlcv.load_bars(conn, "USDJPY", "1m")) == 30


def test_get_bars_falls_back_to_cache(tmp_path):
    conn, p = _provider(tmp_path)
    ohlcv.upsert_bars(conn, _fresh_bars())
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("down")):
        bars = p.get_bars("USDJPY", "1m", 1)
    assert len(bars) == 30


def test_latest_1m_bar_none_on_unhealthy(tmp_path):
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("down")):
        assert p.latest_1m_bar("USDJPY") is None


def test_spec_builtin(tmp_path):
    _, p = _provider(tmp_path)
    assert p.spec("USDJPY").pip_size == 0.01
    assert p.spec("EURUSD").pip_size == 0.0001
    assert p.spec("USDJPY").contract_size == 100_000
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/datafeed/test_price_provider.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/datafeed/price_provider.py`:

```python
"""PriceProvider — ソース解決 (MT5→TD→yfinance)・健全性検証・SQLite キャッシュ。設計書 §5。"""
from __future__ import annotations

import logging
import os
import sqlite3

from agentic_fx.config import Settings
from agentic_fx.core.contracts import Bar, Clock, InstrumentSpec, Quote
from agentic_fx.datafeed import sources
from agentic_fx.datafeed.health import (
    DataUnhealthy, validate_bars, validate_quote,
)
from agentic_fx.store import ohlcv

_log = logging.getLogger("agentic_fx.price")

_SPECS = {
    "USDJPY": InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01, 100_000),
    "EURUSD": InstrumentSpec("EURUSD", 0.0001, 0.01, 50.0, 0.01, 100_000),
}


class PriceProvider:
    def __init__(self, conn: sqlite3.Connection, settings: Settings,
                 clock: Clock) -> None:
        self.conn = conn
        self.settings = settings
        self.clock = clock

    # ---- quote ----------------------------------------------------------

    def get_quote(self, pair: str) -> Quote:
        errors: list[str] = []
        for name, fn in self._quote_chain(pair):
            try:
                q = fn()
                validate_quote(q, self.clock.now(),
                               self.settings.datafeed.freshness_max_min)
                return q
            except Exception as e:  # noqa: BLE001 — 次ソースへ
                _log.warning("quote source %s failed for %s: %s", name, pair, e)
                errors.append(f"{name}: {e}")
        raise DataUnhealthy(f"all quote sources failed for {pair}: {errors}")

    def _quote_chain(self, pair: str):
        d = self.settings.datafeed
        chain = []
        if d.mt5.enabled:
            chain.append(("mt5",
                          lambda: sources.mt5_quote(d.mt5.bridge_url, pair)))
        chain.append(("yfinance", lambda: sources.yf_quote(pair)))
        return chain

    # ---- bars -----------------------------------------------------------

    def get_bars(self, pair: str, interval: str,
                 lookback_days: int = 5) -> list[Bar]:
        d = self.settings.datafeed
        now = self.clock.now()
        interval_min = sources.INTERVAL_MIN[interval]
        errors: list[str] = []
        chain = []
        if d.mt5.enabled:
            chain.append(("mt5", lambda: sources.mt5_bars(
                d.mt5.bridge_url, pair, interval, lookback_days)))
        if d.twelvedata.enabled and os.environ.get("TWELVEDATA_API_KEY"):
            chain.append(("twelvedata", lambda: sources.td_bars(
                os.environ["TWELVEDATA_API_KEY"], pair, interval,
                lookback_days)))
        chain.append(("yfinance", lambda: sources.yf_bars(
            pair, interval, lookback_days)))

        for name, fn in chain:
            try:
                bars = fn()
                validate_bars(bars, now, d.freshness_max_min * interval_min,
                              interval_min)
                ohlcv.upsert_bars(self.conn, bars)
                return bars
            except Exception as e:  # noqa: BLE001
                _log.warning("bars source %s failed for %s: %s", name, pair, e)
                errors.append(f"{name}: {e}")

        cached = ohlcv.load_bars(self.conn, pair, interval)
        if cached:
            try:
                validate_bars(cached, now,
                              d.freshness_max_min * interval_min, interval_min)
                _log.warning("using cached bars for %s %s", pair, interval)
                return cached
            except DataUnhealthy as e:
                errors.append(f"cache: {e}")
        raise DataUnhealthy(f"all bar sources failed for {pair}: {errors}")

    def latest_1m_bar(self, pair: str) -> Bar | None:
        try:
            return self.get_bars(pair, "1m", 1)[-1]
        except DataUnhealthy:
            return None

    # ---- misc -----------------------------------------------------------

    def spec(self, pair: str) -> InstrumentSpec:
        try:
            return _SPECS[pair]
        except KeyError:
            raise DataUnhealthy(f"no instrument spec for {pair}") from None

    def healthcheck(self, pair: str) -> str:
        return self.get_quote(pair).source
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/datafeed/test_price_provider.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/datafeed/price_provider.py tests/datafeed/test_price_provider.py
git commit -m "feat: PriceProvider (ソース解決・健全性・キャッシュフォールバック・組み込み spec)"
```

---

### Task 4: 指標 + MTF (datafeed/indicators.py)

**Files:**
- Create: `src/agentic_fx/datafeed/indicators.py`
- Test: `tests/datafeed/test_indicators.py`

**Interfaces:**
- Produces:
  - `bars_to_df(bars: list[Bar]) -> pd.DataFrame` — index=ts, columns open/high/low/close/volume
  - `resample(df: pd.DataFrame, rule: str) -> pd.DataFrame` — `"4h"` 等へ OHLCV リサンプル (MTF)
  - `compute_indicators(df: pd.DataFrame) -> dict` — 組み込み指標: `sma_20, sma_50, ema_12, ema_26, rsi_14, atr_14, macd, macd_signal, bb_upper, bb_lower` (各 float、直近値)。データ不足の指標は None
- 前身の technical_scorer (判断系) は移植しない — 生の指標値だけを返し、判断は LLM (設計書 §2)

- [ ] **Step 1: 失敗するテストを書く**

`tests/datafeed/test_indicators.py`:

```python
import math
from datetime import datetime, timedelta, timezone

from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.indicators import (
    bars_to_df, compute_indicators, resample,
)

NOW = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)


def _bars(n=100):
    out = []
    for i in range(n):
        base = 148.0 + math.sin(i / 10) * 0.5
        out.append(Bar("USDJPY", "1h", NOW + timedelta(hours=i),
                       base, base + 0.1, base - 0.1, base + 0.02, 100))
    return out


def test_bars_to_df_shape():
    df = bars_to_df(_bars(10))
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 10


def test_resample_4h():
    df = bars_to_df(_bars(8))
    r = resample(df, "4h")
    assert len(r) == 2
    assert r.iloc[0]["high"] == df.iloc[0:4]["high"].max()
    assert r.iloc[0]["open"] == df.iloc[0]["open"]


def test_compute_indicators_keys():
    ind = compute_indicators(bars_to_df(_bars(100)))
    for k in ("sma_20", "sma_50", "ema_12", "rsi_14", "atr_14", "macd",
              "macd_signal", "bb_upper", "bb_lower"):
        assert k in ind and isinstance(ind[k], float)
    assert 0 <= ind["rsi_14"] <= 100
    assert ind["bb_lower"] < ind["sma_20"] < ind["bb_upper"]


def test_insufficient_data_returns_none():
    ind = compute_indicators(bars_to_df(_bars(10)))
    assert ind["sma_50"] is None
    assert ind["sma_20"] is None or isinstance(ind["sma_20"], float)
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/datafeed/test_indicators.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/datafeed/indicators.py`:

```python
"""組み込みテクニカル指標 + MTF リサンプル。判断はせず生の値のみ返す。"""
from __future__ import annotations

import pandas as pd

from agentic_fx.core.contracts import Bar


def bars_to_df(bars: list[Bar]) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": [b.open for b in bars], "high": [b.high for b in bars],
         "low": [b.low for b in bars], "close": [b.close for b in bars],
         "volume": [b.volume for b in bars]},
        index=pd.DatetimeIndex([b.ts for b in bars]))


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last",
         "volume": "sum"}).dropna()


def _last(series: pd.Series, min_len: int) -> float | None:
    if len(series.dropna()) < 1 or len(series) < min_len:
        return None
    v = series.iloc[-1]
    return None if pd.isna(v) else float(v)


def compute_indicators(df: pd.DataFrame) -> dict:
    close, high, low = df["close"], df["high"], df["low"]
    out: dict[str, float | None] = {}
    out["sma_20"] = _last(close.rolling(20).mean(), 20)
    out["sma_50"] = _last(close.rolling(50).mean(), 50)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    out["ema_12"] = _last(ema12, 12)
    out["ema_26"] = _last(ema26, 26)
    macd = ema12 - ema26
    out["macd"] = _last(macd, 26)
    out["macd_signal"] = _last(macd.ewm(span=9, adjust=False).mean(), 26)

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, pd.NA)
    out["rsi_14"] = _last(100 - 100 / (1 + rs), 15)

    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    out["atr_14"] = _last(tr.rolling(14).mean(), 15)

    std20 = close.rolling(20).std()
    sma20 = close.rolling(20).mean()
    out["bb_upper"] = _last(sma20 + 2 * std20, 20)
    out["bb_lower"] = _last(sma20 - 2 * std20, 20)
    return out
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/datafeed/test_indicators.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/datafeed/indicators.py tests/datafeed/test_indicators.py
git commit -m "feat: 組み込み指標 (SMA/EMA/RSI/ATR/MACD/BB) + MTF リサンプル"
```

---

### Task 5: news fetcher (datafeed/fetchers.py)

**Files:**
- Create: `src/agentic_fx/datafeed/fetchers.py`
- Test: `tests/datafeed/test_fetchers.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Article(url: str, title: str, body: str, published: datetime | None, source_name: str)`
  - `fetch_feed(url: str, source_name: str) -> list[Article]` — feedparser。entry の title + summary を body に。published は struct_time から UTC 変換
  - `fetch_web(url: str, source_name: str) -> list[Article]` — httpx GET + trafilatura 本文抽出で **1 記事** (単一ページ)。抽出失敗は空リスト
- fetcher はこの 2 つのみ (設計書 §6: feed / web、plugin ではない)

- [ ] **Step 1: 失敗するテストを書く**

`tests/datafeed/test_fetchers.py`:

```python
from unittest.mock import MagicMock, patch

from agentic_fx.datafeed.fetchers import Article, fetch_feed, fetch_web

FEED_XML_PARSED = MagicMock()
FEED_XML_PARSED.entries = [
    MagicMock(link="https://ex.com/a1", title="Dollar rallies",
              summary="USD up on CPI",
              published_parsed=(2026, 7, 22, 10, 0, 0, 2, 203, 0)),
    MagicMock(link="https://ex.com/a2", title="BOJ holds", summary="",
              published_parsed=None),
]


def test_fetch_feed_maps_entries():
    with patch("feedparser.parse", return_value=FEED_XML_PARSED):
        arts = fetch_feed("https://ex.com/rss", "example")
    assert len(arts) == 2
    assert arts[0].title == "Dollar rallies"
    assert arts[0].source_name == "example"
    assert arts[0].published is not None
    assert arts[1].published is None


def test_fetch_web_extracts_body():
    resp = MagicMock(text="<html>...</html>")
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp), \
         patch("trafilatura.extract", return_value="本文テキスト"), \
         patch("trafilatura.extract_metadata") as meta:
        meta.return_value = MagicMock(title="記事タイトル")
        arts = fetch_web("https://ex.com/page", "example")
    assert len(arts) == 1
    assert arts[0].body == "本文テキスト"


def test_fetch_web_extract_failure_returns_empty():
    resp = MagicMock(text="<html></html>")
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp), \
         patch("trafilatura.extract", return_value=None):
        assert fetch_web("https://ex.com/page", "example") == []
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/datafeed/test_fetchers.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/datafeed/fetchers.py`:

```python
"""組み込み news fetcher: feed / web の 2 つのみ (設計書 §6)。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
import httpx
import trafilatura


@dataclass(frozen=True, slots=True)
class Article:
    url: str
    title: str
    body: str
    published: datetime | None
    source_name: str


def fetch_feed(url: str, source_name: str) -> list[Article]:
    parsed = feedparser.parse(url)
    out: list[Article] = []
    for e in parsed.entries:
        published = None
        if getattr(e, "published_parsed", None):
            published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        out.append(Article(url=e.link, title=e.title,
                           body=getattr(e, "summary", "") or "",
                           published=published, source_name=source_name))
    return out


def fetch_web(url: str, source_name: str) -> list[Article]:
    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    body = trafilatura.extract(r.text)
    if not body:
        return []
    meta = trafilatura.extract_metadata(r.text)
    title = (meta.title if meta and meta.title else url)
    return [Article(url=url, title=title, body=body, published=None,
                    source_name=source_name)]
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/datafeed/test_fetchers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/datafeed/fetchers.py tests/datafeed/test_fetchers.py
git commit -m "feat: news fetcher (feed/web の 2 組み込みのみ)"
```

---

### Task 6: ChromaDB RAG (store/rag.py)

**Files:**
- Create: `src/agentic_fx/store/rag.py`
- Test: `tests/store/test_rag.py`

**Interfaces:**
- Produces: `class Rag`:
  - `__init__(self, data_dir: Path)` — `chromadb.PersistentClient(path=data_dir)`、コレクション `news` / `reflections` (embedding は chromadb デフォルト)
  - `add_news(self, articles: list[dict], now: datetime) -> int` — dict keys: url/title/body/source_name/published。**url をIDにして重複 upsert**。metadata に `added_at` (ISO)。追加件数を返す
  - `search_news(self, query: str, n: int = 5) -> list[dict]` — `{url, title, body, source_name}` のリスト
  - `cleanup_news(self, now: datetime, hours: int = 48) -> int` — `added_at` が閾値より古いものを削除、削除件数を返す (設計書 §12)
  - `add_reflection(self, order_id: int, content: str, pair: str) -> None` / `search_reflections(self, query: str, n: int = 5) -> list[dict]`

- [ ] **Step 1: 失敗するテストを書く**

`tests/store/test_rag.py`:

```python
from datetime import datetime, timedelta, timezone

from agentic_fx.store.rag import Rag

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

ARTS = [
    {"url": "https://ex.com/a1", "title": "Dollar rallies on CPI",
     "body": "The US dollar strengthened after CPI data.",
     "source_name": "ex", "published": None},
    {"url": "https://ex.com/a2", "title": "BOJ keeps rates",
     "body": "Bank of Japan kept interest rates unchanged.",
     "source_name": "ex", "published": None},
]


def test_add_and_search(tmp_path):
    rag = Rag(tmp_path / "rag")
    assert rag.add_news(ARTS, NOW) == 2
    hits = rag.search_news("US dollar CPI", n=1)
    assert len(hits) == 1
    assert hits[0]["url"] in {"https://ex.com/a1", "https://ex.com/a2"}


def test_upsert_dedup_by_url(tmp_path):
    rag = Rag(tmp_path / "rag")
    rag.add_news(ARTS, NOW)
    rag.add_news(ARTS, NOW)  # 再投入
    assert rag.count_news() == 2


def test_cleanup_removes_old(tmp_path):
    rag = Rag(tmp_path / "rag")
    rag.add_news([ARTS[0]], NOW - timedelta(hours=50))
    rag.add_news([ARTS[1]], NOW)
    removed = rag.cleanup_news(NOW, hours=48)
    assert removed == 1
    assert rag.count_news() == 1


def test_reflections_roundtrip(tmp_path):
    rag = Rag(tmp_path / "rag")
    rag.add_reflection(1, "USDJPY long был stopped out due to CPI spike",
                       "USDJPY")
    hits = rag.search_reflections("stopped out CPI", n=1)
    assert hits and hits[0]["order_id"] == 1
```

(補助として `count_news(self) -> int` も Produces に含める)

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/store/test_rag.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/store/rag.py`:

```python
"""ChromaDB RAG — news (48h 掃除) / reflections の 2 コレクション (設計書 §12)。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import chromadb


class Rag:
    def __init__(self, data_dir: Path) -> None:
        self._client = chromadb.PersistentClient(path=str(data_dir))
        self._news = self._client.get_or_create_collection("news")
        self._refl = self._client.get_or_create_collection("reflections")

    # ---- news -----------------------------------------------------------

    def add_news(self, articles: list[dict], now: datetime) -> int:
        if not articles:
            return 0
        self._news.upsert(
            ids=[a["url"] for a in articles],
            documents=[f"{a['title']}\n{a['body']}" for a in articles],
            metadatas=[{"url": a["url"], "title": a["title"],
                        "source_name": a["source_name"],
                        "added_at": now.isoformat()} for a in articles])
        return len(articles)

    def search_news(self, query: str, n: int = 5) -> list[dict]:
        if self.count_news() == 0:
            return []
        res = self._news.query(query_texts=[query],
                               n_results=min(n, self.count_news()))
        out = []
        for i, doc in enumerate(res["documents"][0]):
            meta = res["metadatas"][0][i]
            out.append({"url": meta["url"], "title": meta["title"],
                        "body": doc, "source_name": meta["source_name"]})
        return out

    def count_news(self) -> int:
        return self._news.count()

    def cleanup_news(self, now: datetime, hours: int = 48) -> int:
        cutoff = (now - timedelta(hours=hours)).isoformat()
        got = self._news.get(include=["metadatas"])
        old = [i for i, m in zip(got["ids"], got["metadatas"])
               if m["added_at"] < cutoff]
        if old:
            self._news.delete(ids=old)
        return len(old)

    # ---- reflections ----------------------------------------------------

    def add_reflection(self, order_id: int, content: str, pair: str) -> None:
        self._refl.upsert(ids=[str(order_id)], documents=[content],
                          metadatas=[{"order_id": order_id, "pair": pair}])

    def search_reflections(self, query: str, n: int = 5) -> list[dict]:
        if self._refl.count() == 0:
            return []
        res = self._refl.query(query_texts=[query],
                               n_results=min(n, self._refl.count()))
        return [{"order_id": m["order_id"], "pair": m["pair"], "content": d}
                for d, m in zip(res["documents"][0], res["metadatas"][0])]
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/store/test_rag.py -v`
Expected: PASS (初回は embedding モデルの DL で時間がかかる場合あり)

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/store/rag.py tests/store/test_rag.py
git commit -m "feat: ChromaDB RAG (news 48h 掃除 / reflections)"
```

---

### Task 7: news collector + 初期ソースデータ (datafeed/news_collector.py)

**Files:**
- Create: `src/agentic_fx/datafeed/news_collector.py`
- Create: `src/agentic_fx/datafeed/default_sources.py`
- Test: `tests/datafeed/test_news_collector.py`

**Interfaces:**
- Produces:
  - `DEFAULT_SOURCES: list[dict]` — 基本ニュースソース (name/fetcher/url)。素の clone で動く初期データ (設計書 §6)。例: Yahoo Finance RSS・NHK 経済・FXStreet RSS の 3 件 (実装時に到達性を確認して差し替え可)
  - `seed_default_sources(conn, now) -> int` — `news_sources` に **enabled=True, added_by="user"** で未登録分のみ INSERT。登録件数を返す (init から呼ぶ)
  - `class NewsCollector`:
    - `__init__(self, conn, rag: Rag, activity: ActivityLog, clock: Clock)`
    - `collect(self) -> int` — enabled な news_sources を全件回し、fetcher (feed/web) で取得 → RAG upsert → `cleanup_news(48h)` → activity NEWS 記録。**個別ソースの失敗はスキップ + 技術ログ warning** (1 ソースの障害で全体を止めない)。取得記事総数を返す。scheduler の `on_news_cycle` に差し込む

- [ ] **Step 1: 失敗するテストを書く**

`tests/datafeed/test_news_collector.py`:

```python
from datetime import datetime, timezone
from unittest.mock import patch

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import FixedClock
from agentic_fx.datafeed.fetchers import Article
from agentic_fx.datafeed.news_collector import (
    DEFAULT_SOURCES, NewsCollector, seed_default_sources,
)
from agentic_fx.store import news_sources
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

ART = Article(url="https://ex.com/a1", title="t", body="b",
              published=None, source_name="s1")


def _env(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag")
    col = NewsCollector(conn, rag, ActivityLog(tmp_path / "a.log"),
                        FixedClock(NOW))
    return conn, rag, col


def test_seed_defaults_idempotent(tmp_path):
    conn, _, _ = _env(tmp_path)
    n1 = seed_default_sources(conn, NOW)
    assert n1 == len(DEFAULT_SOURCES) >= 2
    assert seed_default_sources(conn, NOW) == 0  # 冪等
    assert all(s["enabled"] for s in news_sources.list_all(conn))


def test_collect_fetches_enabled_sources(tmp_path):
    conn, rag, col = _env(tmp_path)
    news_sources.add(conn, name="s1", fetcher="feed", url="https://ex.com/rss",
                     added_by="user", now=NOW, enabled=True)
    news_sources.add(conn, name="s2", fetcher="feed", url="https://ex.com/off",
                     added_by="user", now=NOW, enabled=False)
    with patch("agentic_fx.datafeed.news_collector.fetch_feed",
               return_value=[ART]) as f:
        total = col.collect()
    f.assert_called_once()          # enabled のみ
    assert total == 1
    assert rag.count_news() == 1


def test_collect_survives_source_failure(tmp_path):
    conn, rag, col = _env(tmp_path)
    news_sources.add(conn, name="bad", fetcher="feed", url="https://bad",
                     added_by="user", now=NOW, enabled=True)
    news_sources.add(conn, name="good", fetcher="web", url="https://good",
                     added_by="user", now=NOW, enabled=True)
    with patch("agentic_fx.datafeed.news_collector.fetch_feed",
               side_effect=OSError("down")), \
         patch("agentic_fx.datafeed.news_collector.fetch_web",
               return_value=[ART]):
        total = col.collect()
    assert total == 1  # bad はスキップ
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/datafeed/test_news_collector.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/datafeed/default_sources.py`:

```python
"""素の clone で動く基本ニュースソース (設計書 §6)。URL は実装時に到達性確認のこと。"""
DEFAULT_SOURCES: list[dict] = [
    {"name": "yahoo-finance-topstories", "fetcher": "feed",
     "url": "https://finance.yahoo.com/news/rssindex"},
    {"name": "nhk-keizai", "fetcher": "feed",
     "url": "https://www3.nhk.or.jp/rss/news/cat5.xml"},
    {"name": "fxstreet-news", "fetcher": "feed",
     "url": "https://www.fxstreet.com/rss/news"},
]
```

`src/agentic_fx/datafeed/news_collector.py`:

```python
"""NewsCollector — news_sources を回して RAG へ。scheduler の on_news_cycle 実装。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.datafeed.default_sources import DEFAULT_SOURCES
from agentic_fx.datafeed.fetchers import fetch_feed, fetch_web
from agentic_fx.store import news_sources
from agentic_fx.store.rag import Rag

_log = logging.getLogger("agentic_fx.news")

__all__ = ["DEFAULT_SOURCES", "NewsCollector", "seed_default_sources"]


def seed_default_sources(conn: sqlite3.Connection, now: datetime) -> int:
    existing = {s["url"] for s in news_sources.list_all(conn)}
    added = 0
    for s in DEFAULT_SOURCES:
        if s["url"] not in existing:
            news_sources.add(conn, name=s["name"], fetcher=s["fetcher"],
                             url=s["url"], added_by="user", now=now,
                             enabled=True)
            added += 1
    return added


class NewsCollector:
    def __init__(self, conn: sqlite3.Connection, rag: Rag,
                 activity: ActivityLog, clock: Clock) -> None:
        self.conn = conn
        self.rag = rag
        self.activity = activity
        self.clock = clock

    def collect(self) -> int:
        now = self.clock.now()
        total = 0
        for src in news_sources.list_enabled(self.conn):
            try:
                fetch = fetch_feed if src["fetcher"] == "feed" else fetch_web
                articles = fetch(src["url"], src["name"])
                total += self.rag.add_news(
                    [{"url": a.url, "title": a.title, "body": a.body,
                      "source_name": a.source_name, "published": a.published}
                     for a in articles], now)
            except Exception as e:  # noqa: BLE001 — 1 ソース障害で止めない
                _log.warning("news source %s failed: %s", src["name"], e)
        removed = self.rag.cleanup_news(now, hours=48)
        self.activity.write(Category.NEWS, "collected",
                            f"{total} articles ({removed} cleaned)")
        return total
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/datafeed/test_news_collector.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/datafeed/news_collector.py \
  src/agentic_fx/datafeed/default_sources.py tests/datafeed/test_news_collector.py
git commit -m "feat: news collector + 基本ソース初期データ (障害スキップ・48h 掃除)"
```

---

### Task 8: econ カレンダー (datafeed/econ_calendar.py)

**Files:**
- Create: `src/agentic_fx/datafeed/econ_calendar.py`
- Test: `tests/datafeed/test_econ_calendar.py`

**Interfaces:**
- Produces:
  - `fetch_ff_calendar() -> list[dict]` — ForexFactory 週間カレンダー JSON (`https://nfs.faireconomy.media/ff_calendar_thisweek.json`) を httpx GET し `{ts, country, name, importance, forecast, previous}` に整形 (importance: High=3/Medium=2/Low=1)
  - `class EconCalendar`:
    - `__init__(self, conn, activity: ActivityLog, clock: Clock)`
    - `refresh(self) -> int` — fetch → `econ_events.upsert` 全件。失敗は 0 件 + 技術ログ warning (fail soft — カレンダーは判断材料であり執行系ではない)
    - `upcoming(self, hours: int = 24) -> list[dict]` — store 委譲

- [ ] **Step 1: 失敗するテストを書く**

`tests/datafeed/test_econ_calendar.py`:

```python
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import FixedClock
from agentic_fx.datafeed.econ_calendar import EconCalendar, fetch_ff_calendar
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

FF_JSON = [
    {"title": "CPI y/y", "country": "USD", "date": "2026-07-22T15:30:00-04:00",
     "impact": "High", "forecast": "3.1%", "previous": "3.0%"},
    {"title": "Retail Sales", "country": "GBP",
     "date": "2026-07-23T02:00:00-04:00", "impact": "Medium",
     "forecast": "", "previous": "0.2%"},
]


def test_fetch_maps_fields():
    resp = MagicMock()
    resp.json.return_value = FF_JSON
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp):
        events = fetch_ff_calendar()
    assert events[0]["country"] == "USD"
    assert events[0]["importance"] == 3
    assert events[1]["importance"] == 2
    assert events[0]["ts"].tzinfo is not None


def test_refresh_upserts_and_upcoming(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    cal = EconCalendar(conn, ActivityLog(tmp_path / "a.log"), FixedClock(NOW))
    resp = MagicMock()
    resp.json.return_value = FF_JSON
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp):
        assert cal.refresh() == 2
        assert cal.refresh() == 2  # upsert 冪等
    rows = cal.upcoming(hours=24)
    assert len(rows) == 1  # USD CPI (22 日 19:30 UTC) のみ 24h 以内
    assert rows[0]["name"] == "CPI y/y"


def test_refresh_fail_soft(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    cal = EconCalendar(conn, ActivityLog(tmp_path / "a.log"), FixedClock(NOW))
    with patch("httpx.get", side_effect=OSError("down")):
        assert cal.refresh() == 0  # 例外を漏らさない
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/datafeed/test_econ_calendar.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/datafeed/econ_calendar.py`:

```python
"""経済指標カレンダー (ForexFactory 週間 JSON)。fail soft — 執行系ではない。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

import httpx

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.store import econ_events

_log = logging.getLogger("agentic_fx.econ")
_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_IMPACT = {"High": 3, "Medium": 2, "Low": 1}


def fetch_ff_calendar() -> list[dict]:
    r = httpx.get(_URL, timeout=30)
    r.raise_for_status()
    out = []
    for e in r.json():
        out.append({"ts": datetime.fromisoformat(e["date"]),
                    "country": e["country"], "name": e["title"],
                    "importance": _IMPACT.get(e.get("impact"), 0),
                    "forecast": e.get("forecast") or None,
                    "previous": e.get("previous") or None})
    return out


class EconCalendar:
    def __init__(self, conn: sqlite3.Connection, activity: ActivityLog,
                 clock: Clock) -> None:
        self.conn = conn
        self.activity = activity
        self.clock = clock

    def refresh(self) -> int:
        try:
            events = fetch_ff_calendar()
        except Exception as e:  # noqa: BLE001
            _log.warning("econ calendar fetch failed: %s", e)
            return 0
        for ev in events:
            econ_events.upsert(self.conn, ts=ev["ts"], country=ev["country"],
                               name=ev["name"], importance=ev["importance"],
                               forecast=ev["forecast"],
                               previous=ev["previous"])
        self.activity.write(Category.NEWS, "econ_refreshed",
                            f"{len(events)} events")
        return len(events)

    def upcoming(self, hours: int = 24) -> list[dict]:
        return econ_events.upcoming(self.conn, self.clock.now(), hours)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/datafeed/test_econ_calendar.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/datafeed/econ_calendar.py tests/datafeed/test_econ_calendar.py
git commit -m "feat: econ カレンダー (ForexFactory 週間 JSON、fail soft)"
```

---

### Task 9: init 拡張 (価格ソース接続確認) + 仕上げ

**Files:**
- Modify: `src/agentic_fx/service.py`
- Modify: `tests/test_init_and_guard.py`

**Interfaces:**
- Produces: `run_init` の末尾に接続確認ステップを追加:
  - `seed_default_sources(conn, now)` で基本ニュースソース投入
  - 価格ソース確認: `PriceProvider.healthcheck(pairs[0])` — 成功なら「価格ソース OK (source=...)」表示、`DataUnhealthy` は**警告表示のみで init は成功させる** (オフライン環境でも init 完了できる。サービス起動後の fail closed が本防御線)

- [ ] **Step 1: 失敗するテストを追記**

`tests/test_init_and_guard.py` に追記:

```python
def test_init_seeds_news_sources_and_checks_price(tmp_path, capsys):
    from unittest.mock import patch

    from agentic_fx.store import news_sources
    from agentic_fx.store.db import connect

    _example(tmp_path)
    with patch("agentic_fx.service.PriceProvider") as mock_pp:
        mock_pp.return_value.healthcheck.return_value = "yfinance"
        assert run_init(tmp_path) == 0
    conn = connect(tmp_path / "data" / "agentic.db")
    assert len(news_sources.list_enabled(conn)) >= 2
    assert "yfinance" in capsys.readouterr().out


def test_init_survives_price_check_failure(tmp_path):
    from unittest.mock import patch

    from agentic_fx.datafeed.health import DataUnhealthy

    _example(tmp_path)
    with patch("agentic_fx.service.PriceProvider") as mock_pp:
        mock_pp.return_value.healthcheck.side_effect = DataUnhealthy("all down")
        assert run_init(tmp_path) == 0  # 警告のみで成功
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_init_and_guard.py -v`
Expected: 新規 2 件 FAIL

- [ ] **Step 3: 実装**

`src/agentic_fx/service.py` — import 追加:

```python
from agentic_fx.core.contracts import Mode
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.datafeed.news_collector import seed_default_sources
from agentic_fx.datafeed.price_provider import PriceProvider
```

`run_init` の `init_db(conn)` の直後・state 更新の前に挿入:

```python
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    seeded = seed_default_sources(conn, now)
    if seeded:
        print(f"基本ニュースソースを {seeded} 件登録しました")

    class _Now:
        def now(self):
            return datetime.now(timezone.utc)

    try:
        src = PriceProvider(conn, settings, _Now()).healthcheck(
            settings.pairs[0])
        print(f"価格ソース OK (source={src})")
    except DataUnhealthy as e:
        print(f"警告: 価格ソースに接続できません ({e})。"
              "サービス起動後は fail closed で保護されます。")
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_init_and_guard.py -v && uv run pytest -v`
Expected: 全件 PASS

- [ ] **Step 5: セルフレビュー**

- プラン 2 の注入点シグネチャに適合するか: `PriceProvider.get_quote` → `quote_fn`、`.spec` → `spec_fn`、`.latest_1m_bar` → `bars_fn`、`NewsCollector.collect` → `on_news_cycle`
- 外部アクセスするテストがないか: `grep -rn "httpx.get\|yfinance.download\|feedparser.parse" tests/` が全部 mock 経由

- [ ] **Step 6: Commit**

```bash
git add src/agentic_fx/service.py tests/test_init_and_guard.py
git commit -m "feat: init に基本ニュースソース投入 + 価格ソース接続確認 (警告のみ)"
```

---

## プラン 4/5 への引き継ぎ事項

- ツール実装 (プラン 4) は本プランの API を叩く薄いラッパーにする: `get_ohlcv` → `PriceProvider.get_bars` + `bars_to_df`、`get_indicators` → `compute_indicators` (+ resample で MTF)、`search_news` → `Rag.search_news`、`get_econ_calendar` → `EconCalendar.upcoming`
- trade_loop (プラン 5) の fail closed: Mission 起動前に `PriceProvider.healthcheck` を呼び、`DataUnhealthy` なら Mission を実行せず activity SYSTEM `data_unhealthy` を記録
- equity 時価評価: scheduler tick に「open ポジションの `latest_1m_bar` close で含み損益を計算し snapshot 記録」を追加する (プラン 5 の service 配線時)
- `DEFAULT_SOURCES` の URL は実装時に実際の到達性を確認して差し替えること (テストはモックのため URL の生死に依存しない)
