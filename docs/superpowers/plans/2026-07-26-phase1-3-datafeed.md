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

> **実装済み (2026-07-27、commits `8c846d8..ae656a2`)。以下のコードは実装と一致しません** — レビューで欠陥が見つかり、ユーザー裁定で書き換えました。**現在の正は `src/agentic_fx/datafeed/health.py`** です。変更点:
>
> 1. **`_spans_market_close` を廃止し `_closed_minutes` に置換**。旧実装は休場を跨ぐ区間の連続性チェックを**丸ごと免除**しており、週明けにフィードが止まって 24h 欠損していても「健全」と判定する fail-open だった。新実装は休場だった分**だけ**を差し引き、実際に開場していたはずの欠損本数で判定する
> 2. **サンプリング粒度を `interval_min` に追従**させ、反復回数が `_MAX_CLOSED_TIME_SAMPLES` を超えたら fail closed に倒す (「数え切れない = 健全と断言できない」)
> 3. **ギャップ検査を直近の窓に限定** (`_GAP_CHECK_WINDOW_BARS` / `_GAP_CHECK_WINDOW_MIN_MINUTES`)。検査が答えるべきは「フィードは今動いているか」であり、回復済みの過去の穴で取引を止めない。末尾隣接の穴は常に窓内に入るため、①の検出力は落ちない
> 4. **`as_utc` で naive datetime を弾く** (規約どおり `ValueError`)
>
> **known issue (受容済み)**: `market_hours` が祝日カレンダーを持たないため、祝日隣接週は市場再開後**最大 24h** `DataUnhealthy` が継続する。fail closed 側の誤検知であり Phase 1 (ペーパー) では実損がない。祝日カレンダーの要否はプラン 3 完了後に判断する。

**Files:**
- Create: `src/agentic_fx/datafeed/__init__.py`, `src/agentic_fx/datafeed/health.py`
- Test: `tests/datafeed/__init__.py`, `tests/datafeed/test_health.py`

**Interfaces:**
- Produces:
  - `DataUnhealthy(Exception)` — メッセージに理由を含む
  - `validate_quote(quote: Quote, now: datetime, freshness_max_min: float) -> None` — 鮮度超過・非正値・bid>ask で `DataUnhealthy`
  - `validate_bars(bars: list[Bar], now: datetime, freshness_max_min: float, interval_min: float) -> None` — 空・最終バー鮮度・連続性 (欠損 3 本超)・異常値 (0/NaN/前バー比 ±10% スパイク) で `DataUnhealthy`
  - **バー timestamp の定義: バーの開始時刻**。最終バー鮮度の許容 = `interval_min + freshness_max_min` (確定直後のバーを stale 扱いしない)
  - **連続性の判定は市場休場を欠損としない**: ギャップ区間に市場クローズ時間 (週末) が含まれる場合はスキップ (`market_hours.is_market_open` を 1 時間刻みでサンプリング)

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
    del bars[10:14]  # 4 本欠損 (市場オープン中)
    with pytest.raises(DataUnhealthy, match="gap"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)


def test_weekend_gap_is_not_a_gap():
    # 金 20:00 のバー → 日 21:00 再開のバー: 休場ギャップは欠損ではない
    fri = datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)
    sun = datetime(2026, 7, 26, 21, 0, tzinfo=timezone.utc)
    bars = [Bar("USDJPY", "1h", fri + timedelta(hours=i),
                148.0, 148.1, 147.9, 148.05, 100) for i in range(3)]
    bars += [Bar("USDJPY", "1h", sun + timedelta(hours=i),
                 148.0, 148.1, 147.9, 148.05, 100) for i in range(3)]
    validate_bars(bars, sun + timedelta(hours=3), freshness_max_min=90,
                  interval_min=60)  # 例外なし


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
"""データ健全性検証 — 「取得成功」でなくこの検証の通過がフォールバック採用条件 (設計書 §5)。

バー timestamp はバーの開始時刻とする。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from agentic_fx.core import market_hours
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


def _spans_market_close(a: datetime, b: datetime) -> bool:
    """区間 [a, b] に市場クローズ時間が含まれるか (1 時間刻みサンプリング)。"""
    cur = a
    while cur <= b:
        if not market_hours.is_market_open(cur):
            return True
        cur += timedelta(hours=1)
    return not market_hours.is_market_open(b)


def validate_bars(bars: list[Bar], now: datetime, freshness_max_min: float,
                  interval_min: float) -> None:
    if not bars:
        raise DataUnhealthy("empty bars")
    # ts はバー開始時刻: 確定直後を stale にしないため interval 分を許容に足す
    allowed = timedelta(minutes=freshness_max_min + interval_min)
    if now - bars[-1].ts > allowed:
        raise DataUnhealthy(f"bars stale: last={bars[-1].ts}")
    prev = None
    for b in bars:
        vals = (b.open, b.high, b.low, b.close)
        if any(v <= 0 or not math.isfinite(v) for v in vals):
            raise DataUnhealthy(f"anomalous bar (zero/NaN) at {b.ts}")
        if prev is not None:
            gap = (b.ts - prev.ts).total_seconds() / 60 / interval_min
            if gap > _MAX_GAP_BARS and not _spans_market_close(prev.ts, b.ts):
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
  - `vendor_symbol(logical: str, vendor: str) -> str` — **論理シンボル → vendor 別シンボルの明示 resolver** (`VENDOR_SYMBOLS` テーブル)。Phase 1 は USDJPY / EURUSD (+ 関連指標の拡張点として DXY の例)。未定義は `KeyError` (provider 側でソース失敗として扱う)
  - `yf_quote(pair) -> Quote` / `yf_bars(pair, interval, lookback_days) -> list[Bar]` — yfinance。**`multi_level_index=False` を明示** (現行 yfinance は単一 ticker でも MultiIndex 列がデフォルト — #critical)。quote は直近 1m バーの close を bid=ask として返す (yfinance に板がないため。source="yfinance")
  - `mt5_quote(bridge_url, pair) -> Quote` / `mt5_bars(bridge_url, pair, interval, lookback_days) -> list[Bar]` / **`mt5_bars_range(bridge_url, pair, interval, start, end) -> list[Bar]`** — httpx GET。**実 bridge の API に厳密に合わせること** (下記)。source="mt5"

    **重要 — 既存 bridge の実 API 仕様** (`~/project/finance/mt5_bridge/server.py` を実測。旧記述は実在しない `/rates?symbol&timeframe&count` を叩いており、着手した瞬間に 404/401 で全滅する):

    | | 実際の仕様 |
    |---|---|
    | quote | `GET {bridge_url}/quote/{symbol}` — **symbol はパスパラメータ** (クエリではない) |
    | quote 応答 | `{symbol, bid, ask, spread_points, time}` (`time` は ISO 8601 文字列)。**`spread_points` で実 spread が取れる** |
    | bars | `GET {bridge_url}/ohlcv/{symbol}?from=<ISO>&to=<ISO>&interval=<tf>` — **count ではなく期間指定** |
    | bars 応答 | `{symbol, interval, bars: [...]}` — **`r.json()["bars"]` を読む** (素の配列ではない) |
    | bar の要素 | **`{time, open, high, low, close, volume}`** (`ohlcv_models.OhlcvBar`)。**`tick_volume` ではない** — bridge の client が MT5 の `tick_volume` を `volume` に変換して返すため、`tick_volume` を読むと**全バーの出来高が 0 になる** |
    | interval | `1m \| 5m \| 15m \| 30m \| 1h \| 4h \| 1d` — **4h もネイティブ対応** (`TIMEFRAME_H4`) |
    | 認証 | **`X-Bridge-Api-Key` ヘッダ必須** (bridge 側 `auth_required` が真のとき)。値は `.env` の `MT5_BRIDGE_API_KEY` から `os.environ.get` で読む (秘密情報は `.env` のみ — 制約) |
    | エラー | MT5 未接続 503 / 不正 symbol・期間 400 / symbol 不明 404 |

    - **期間指定が本来の形**なので `mt5_bars_range` を素の関数とし、`mt5_bars(..., lookback_days)` はそれを呼ぶ薄いラッパーにする。この形にしておくと、Phase 2 の長期 backfill (バックテスト用の履歴取り込み) が**新しい経路を作らずそのまま使える**
  - `td_quote(api_key, pair) -> Quote` / `td_bars(api_key, pair, interval, lookback_days) -> list[Bar]` — Twelve Data (`/quote` は実在する — quote チェーンにも入る)。**interval は `_TD_INTERVAL` で変換** (`1m→1min` 等)。`outputsize` は **5000 上限で clamp**
  - `INTERVAL_MIN: dict[str, float]` = `{"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}`
  - **`NATIVE_INTERVALS: dict[str, frozenset[str]]`** — ソース毎にネイティブ対応する足。**取引の時間軸は固定しない**方針 (day / swing で勝てる時間軸を取りに行く。時間軸特化の strategy plugin も許す) のため、足を 1h 決め打ちにせずソースの能力として持つ:

    | source | ネイティブ対応 |
    |---|---|
    | `mt5` | `1m 5m 15m 30m 1h 4h 1d` (bridge の `TIMEFRAME_*` マップと一致させる) |
    | `twelvedata` | `_TD_INTERVAL` のキー |
    | `yfinance` | `1m 5m 15m 1h 1d` (**4h / 30m は無い**) |

  - **ネイティブに無い足は、より細かい足から `resample` で導出する** (例: yfinance の 4h は 1h から)。導出はソース層の責務にせず `PriceProvider` が行い、**どちらの経路で得た足かを記録する** — バックテストと実運用で違う 4h を見ると、バックテストの意味が失われるため

- [ ] **Step 1: 失敗するテストを書く**

`tests/datafeed/test_sources.py`:

```python
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from agentic_fx.datafeed.sources import (
    INTERVAL_MIN, NATIVE_INTERVALS, mt5_bars, mt5_bars_range, mt5_quote,
    td_bars, td_quote, vendor_symbol, yf_bars, yf_quote,
)

IDX = pd.DatetimeIndex(
    [datetime(2026, 7, 22, 11, 58, tzinfo=timezone.utc),
     datetime(2026, 7, 22, 11, 59, tzinfo=timezone.utc)])
_DATA = {"Open": [148.4, 148.45], "High": [148.5, 148.55],
         "Low": [148.3, 148.4], "Close": [148.45, 148.5],
         "Volume": [100, 120]}
DF = pd.DataFrame(_DATA, index=IDX)
# 現行 yfinance デフォルトの (Price, Ticker) MultiIndex 列も検証する
DF_MULTI = pd.DataFrame(
    {(k, "USDJPY=X"): v for k, v in _DATA.items()}, index=IDX)
DF_MULTI.columns = pd.MultiIndex.from_tuples(DF_MULTI.columns)


def test_vendor_symbol_resolver():
    assert vendor_symbol("USDJPY", "yf") == "USDJPY=X"
    assert vendor_symbol("USDJPY", "td") == "USD/JPY"
    assert vendor_symbol("USDJPY", "mt5") == "USDJPY"
    with pytest.raises(KeyError):
        vendor_symbol("GBPUSD", "yf")  # 未定義は KeyError


def test_yf_bars_maps_dataframe():
    with patch("yfinance.download", return_value=DF) as dl:
        bars = yf_bars("USDJPY", "1m", 1)
    assert dl.call_args[0][0] == "USDJPY=X"
    assert dl.call_args[1]["multi_level_index"] is False
    assert len(bars) == 2
    assert bars[-1].close == 148.5


def test_yf_bars_normalizes_multiindex():
    # multi_level_index=False が効かない旧版でも列を単層化して読めること
    with patch("yfinance.download", return_value=DF_MULTI):
        bars = yf_bars("USDJPY", "1m", 1)
    assert len(bars) == 2
    assert bars[-1].close == 148.5


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


def test_td_quote():
    resp = MagicMock()
    resp.json.return_value = {"symbol": "USD/JPY", "bid": "148.49",
                              "ask": "148.51",
                              "timestamp": 1784721570}
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp) as g:
        q = td_quote("key", "USDJPY")
    assert g.call_args[1]["params"]["symbol"] == "USD/JPY"
    assert q.bid == 148.49 and q.source == "twelvedata"
    assert q.ts.tzinfo is not None


def test_td_bars_parses_and_maps_interval():
    resp = MagicMock()
    resp.json.return_value = {"values": [
        {"datetime": "2026-07-22 11:59:00", "open": "148.45",
         "high": "148.55", "low": "148.40", "close": "148.50"},
        {"datetime": "2026-07-22 11:58:00", "open": "148.40",
         "high": "148.50", "low": "148.30", "close": "148.45"},
    ]}
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp) as g:
        bars = td_bars("key", "USDJPY", "1m", 30)
    params = g.call_args[1]["params"]
    assert params["interval"] == "1min"      # TD の interval 表記へ変換
    assert params["outputsize"] <= 5000      # API 上限で clamp
    assert params["symbol"] == "USD/JPY"
    assert bars[0].ts < bars[1].ts           # 昇順に並べ替え
    assert bars[-1].close == 148.5


def test_interval_table_covers_all_supported_timeframes():
    """取引の時間軸を固定しないため、4h / 30m も表に載る (spec §5)。"""
    assert INTERVAL_MIN["1h"] == 60
    assert INTERVAL_MIN["4h"] == 240
    assert INTERVAL_MIN["30m"] == 30


def test_native_intervals_reflect_source_capability():
    """足の可否はシステムの仕様ではなくソースの能力として持つ。"""
    assert "4h" in NATIVE_INTERVALS["mt5"]        # MT5 は H4 をネイティブに持つ
    assert "4h" not in NATIVE_INTERVALS["yfinance"]  # yfinance は持たない
    assert "30m" not in NATIVE_INTERVALS["yfinance"]
    # 全ソースのネイティブ足は INTERVAL_MIN に載っていること
    for src, ivs in NATIVE_INTERVALS.items():
        assert ivs <= set(INTERVAL_MIN), f"{src} に未知の足がある"
```

注: `Bar` は `source` フィールドを持たない (プラン 1 契約)。バーの出所は `PriceProvider.last_bars_source(pair, interval)` (Task 3) で公開する。

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/datafeed/test_sources.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/datafeed/sources.py`:

```python
"""ソース別 fetcher (yfinance / MT5 bridge / Twelve Data)。例外は送出、選択は provider。

論理シンボル → vendor 別シンボルは VENDOR_SYMBOLS で明示解決する
(関連指標 — DXY 等 — の追加はこの表への行追加、設計書 §5)。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
import yfinance

from agentic_fx.core.contracts import Bar, Quote

INTERVAL_MIN: dict[str, float] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}

# _TD_INTERVAL は NATIVE_INTERVALS より先に定義すること (後者が参照する)
_TD_INTERVAL = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
                "1h": "1h", "4h": "4h", "1d": "1day"}
_TD_MAX_OUTPUTSIZE = 5000

# ソース毎のネイティブ対応足。取引の時間軸は固定しないため、足の可否は
# 「システムの仕様」ではなく「ソースの能力」として持つ。ネイティブに無い
# 足は PriceProvider がより細かい足から resample で導出する。
NATIVE_INTERVALS: dict[str, frozenset[str]] = {
    "mt5": frozenset({"1m", "5m", "15m", "30m", "1h", "4h", "1d"}),
    "twelvedata": frozenset(_TD_INTERVAL),
    "yfinance": frozenset({"1m", "5m", "15m", "1h", "1d"}),
}

VENDOR_SYMBOLS: dict[str, dict[str, str]] = {
    "USDJPY": {"yf": "USDJPY=X", "td": "USD/JPY", "mt5": "USDJPY"},
    "EURUSD": {"yf": "EURUSD=X", "td": "EUR/USD", "mt5": "EURUSD"},
    # 関連指標の拡張例 (Phase 1 では未使用):
    "DXY": {"yf": "DX-Y.NYB", "td": "DXY"},
}


def vendor_symbol(logical: str, vendor: str) -> str:
    return VENDOR_SYMBOLS[logical][vendor]


def _flatten_yf_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


def yf_bars(pair: str, interval: str, lookback_days: int) -> list[Bar]:
    df = yfinance.download(
        vendor_symbol(pair, "yf"), interval=interval,
        period=f"{lookback_days}d", progress=False, auto_adjust=False,
        multi_level_index=False)
    df = _flatten_yf_columns(df)
    bars: list[Bar] = []
    for ts, row in df.iterrows():
        t = ts.to_pydatetime()
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        volume = row["Volume"] if "Volume" in row else 0.0
        bars.append(Bar(pair, interval, t, float(row["Open"]),
                        float(row["High"]), float(row["Low"]),
                        float(row["Close"]), float(volume or 0)))
    return bars


def yf_quote(pair: str) -> Quote:
    bars = yf_bars(pair, "1m", 1)
    if not bars:
        raise RuntimeError(f"yfinance returned no data for {pair}")
    last = bars[-1]
    return Quote(pair, last.close, last.close, last.ts, "yfinance")


def _mt5_headers() -> dict[str, str]:
    """bridge の API キー。auth_required が偽なら未設定でも通る。"""
    key = os.environ.get("MT5_BRIDGE_API_KEY")
    return {"X-Bridge-Api-Key": key} if key else {}


def mt5_quote(bridge_url: str, pair: str) -> Quote:
    # symbol はパスパラメータ (クエリではない — 実 bridge 仕様)
    sym = vendor_symbol(pair, "mt5")
    r = httpx.get(f"{bridge_url}/quote/{sym}",
                  headers=_mt5_headers(), timeout=10)
    r.raise_for_status()
    d = r.json()   # {symbol, bid, ask, spread_points, time}
    return Quote(pair, float(d["bid"]), float(d["ask"]),
                 datetime.fromisoformat(d["time"]), "mt5")


def mt5_bars_range(bridge_url: str, pair: str, interval: str,
                   start: datetime, end: datetime) -> list[Bar]:
    """期間指定でバーを取る (bridge の本来の形。copy_rates_range ベース)。

    Phase 2 の長期 backfill もこの関数をそのまま使う。
    """
    sym = vendor_symbol(pair, "mt5")
    r = httpx.get(f"{bridge_url}/ohlcv/{sym}",
                  params={"from": start.isoformat(), "to": end.isoformat(),
                          "interval": interval},
                  headers=_mt5_headers(), timeout=30)
    r.raise_for_status()
    payload = r.json()          # {symbol, interval, bars: [...]}
    # bar の出来高キーは "volume" (bridge が MT5 の tick_volume を変換済み)。
    # "tick_volume" を読むと全バーが 0 になる
    return [Bar(pair, interval, datetime.fromisoformat(x["time"]),
                float(x["open"]), float(x["high"]), float(x["low"]),
                float(x["close"]), float(x["volume"]))
            for x in payload["bars"]]


def mt5_bars(bridge_url: str, pair: str, interval: str,
             lookback_days: int) -> list[Bar]:
    """lookback_days 形式の薄いラッパー (他ソースと同じ呼び口を保つため)。"""
    end = datetime.now(timezone.utc)
    return mt5_bars_range(bridge_url, pair, interval,
                          end - timedelta(days=lookback_days), end)


def td_quote(api_key: str, pair: str) -> Quote:
    r = httpx.get("https://api.twelvedata.com/quote",
                  params={"symbol": vendor_symbol(pair, "td"),
                          "apikey": api_key}, timeout=10)
    r.raise_for_status()
    d = r.json()
    ts = datetime.fromtimestamp(int(d["timestamp"]), tz=timezone.utc)
    return Quote(pair, float(d["bid"]), float(d["ask"]), ts, "twelvedata")


def td_bars(api_key: str, pair: str, interval: str,
            lookback_days: int) -> list[Bar]:
    size = min(_TD_MAX_OUTPUTSIZE,
               int(lookback_days * 1440 / INTERVAL_MIN[interval]))
    r = httpx.get("https://api.twelvedata.com/time_series",
                  params={"symbol": vendor_symbol(pair, "td"),
                          "interval": _TD_INTERVAL[interval],
                          "outputsize": size, "apikey": api_key}, timeout=30)
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
- Create: `src/agentic_fx/datafeed/bars.py` — 足の変換 (Task 4 の indicators も import する共有モジュール)
- Create: `src/agentic_fx/datafeed/price_provider.py`
- Modify: `src/agentic_fx/config.py` — `DatafeedSettings` に `intervals` / `primary_intervals` を追加
- Modify: `config/settings.yaml.example` — 同じキーを追加 (**両方を同期すること** — 規約)
- Test: `tests/datafeed/test_bars.py`, `tests/datafeed/test_price_provider.py`

**Interfaces:**
- Produces: `datafeed/bars.py`:
  - `bars_to_df(bars: list[Bar]) -> pd.DataFrame` — index=ts, columns open/high/low/close/volume
  - `df_to_bars(df: pd.DataFrame, pair: str, interval: str) -> list[Bar]` — 逆変換 (導出足を `Bar` に戻す)
  - `resample(df: pd.DataFrame, rule: str) -> pd.DataFrame` — OHLCV リサンプル。**バケット境界は UTC 固定** (`origin="epoch"`) とし、実運用とバックテストで同じ境界を使う

- Produces: **config への追加** (取引の時間軸を固定しないため):

  ```yaml
  datafeed:
    # ... 既存キー ...
    intervals: [1m, 5m, 15m, 1h, 4h]   # 取得・保持する足。plugin とバックテストが使える足の集合
    primary_intervals: [1h]            # 判断 Mission が依存する足。healthcheck はこれを検証する
  ```

  - `intervals` は **`1m` を必ず含む** (ペーパー約定判定が依存する構造的要件)。含まなければ `ConfigError`
  - `primary_intervals` は **`intervals` の部分集合**かつ空でないこと。違反は `ConfigError`
  - 全要素が `INTERVAL_MIN` のキーであること。違反は `ConfigError`
  - **設計意図**: 判断に使う足を増やしたければ `primary_intervals` に足す。**足を増やすほど単一障害点も増える** (どれか 1 つ不健全なら fail closed で Mission が止まる) ため、既定は `[1h]` に留め、時間軸特化の strategy plugin が使う足は `intervals` 側にだけ入れる

- Produces: `class PriceProvider`:
  - `__init__(self, conn, settings: Settings, clock: Clock)`
  - `get_quote(self, pair: str) -> Quote` — 優先順位 **MT5 → Twelve Data → yfinance** (quote も 3 ソース — TD には `/quote` がある)。**enabled なソースのみ**を順に試し、**取得成功 + `validate_quote` 通過**した最初の Quote を返す。全滅なら `DataUnhealthy`
  - `get_bars(self, pair: str, interval: str, lookback_days: int = 5) -> list[Bar]` — 同順・同条件。**足を決め打ちしない**: ソースが `NATIVE_INTERVALS` で対応していればそのまま要求し、対応していなければ**より細かいネイティブ足を取って `resample` で導出する** (`4h` を持たない yfinance なら 1h → 4h)。健全性通過後 **ohlcv キャッシュに upsert** して返す (導出した足も upsert し、`interval` は要求された足で保存する)。全滅時、キャッシュに健全なバーがあればそれを返し、なければ `DataUnhealthy`。`INTERVAL_MIN` に無い足は `ValueError`
  - `last_bars_source(self, pair: str, interval: str) -> str | None` — 直近の `get_bars` が採用した source 名 (`"mt5" / "twelvedata" / "yfinance" / "cache"`)。**データ品質フラグの公開点** (設計書 §5: yfinance のみで得た判断に品質フラグを残す — trade_loop がこれを missions 記録・activity に添える)
  - `latest_1m_bar(self, pair: str) -> Bar | None` — scheduler の `bars_fn` 用。`DataUnhealthy` は None (fills 判定をスキップさせる)
  - `spec(self, pair: str) -> InstrumentSpec` — executor の `spec_fn` 用。Phase 1 は組み込みテーブル。Phase 3 で MT5 照会に置換
  - `healthcheck(self, pair: str) -> str` — **quote と `settings.datafeed.primary_intervals` の全バー**が健全であることを確認し、quote の source 名を返す。どれか 1 つでも全滅なら `DataUnhealthy` (設計書 §5 の fail closed — quote だけの確認では OHLCV 不健全時に Mission が走ってしまう)。**1h 決め打ちにしない** — 取引の時間軸は固定せず、判断に使う足を設定で選ぶため
  - **`bars_origin(self, pair: str, interval: str) -> str | None`** — 直近に返した足の由来 (`"mt5"` / `"yfinance(1h→4h derived)"` / `"cache"` 等)。導出足かネイティブ足かを記録し、status 表示とバックテストの再現性確認に使う
  - **`quote_to_account_rate(self, quote_ccy: str, account_ccy: str) -> float`** — 「クォート通貨 1 単位 = 口座通貨いくらか」を返す (設計書 §5「口座通貨と換算」)。**この実装により、プラン 2 で暫定的に fail closed にしていたクロス通貨ペア (JPY 口座での EURUSD 等) が扱えるようになる**
    - `quote_ccy == account_ccy` なら `1.0`
    - **①直接ペア `{quote}{account}`** の quote 価格 → そのまま。**②逆ペア `{account}{quote}`** → その逆数。**③どちらも無ければ USD 経由のクロス** (`{quote}USD` × `USD{account}` 相当)
    - 使う quote は `get_quote` 経由なので**鮮度・有限性・正値の検証を必ず通る**。得られない場合は `DataUnhealthy` を送出し、呼び出し側 (sizing) が `SizingError` に変換して **fail closed**。古いレートでのサイジングは無音の過大建玉になるため、ここは握りつぶさないこと
    - レート算出に使うペアは `VENDOR_SYMBOLS` に登録されている必要がある。未登録なら `DataUnhealthy`
- 各ソースの失敗 (例外 / 検証不合格) は技術ログ warning + 次ソースへ。yfinance も `enabled: false` なら試さない (ユーザーの明示的無効化を尊重)

- [ ] **Step 1a: 足の変換 (`datafeed/bars.py`) の失敗するテストを書く**

**先に `bars.py` を作る** — `PriceProvider` の足導出と Task 4 の indicators が両方これに依存する。

`tests/datafeed/test_bars.py`:

```python
from datetime import datetime, timedelta, timezone

from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.bars import bars_to_df, df_to_bars, resample

NOW = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)


def _bars(n=8, interval="1h", start=NOW):
    step = timedelta(hours=1)
    return [Bar("USDJPY", interval, start + step * i,
                148.0 + i, 148.5 + i, 147.5 + i, 148.2 + i, 10.0 + i)
            for i in range(n)]


def test_round_trip_preserves_values_tz_and_interval():
    """bars_to_df → df_to_bars で値・tz・interval が落ちないこと。"""
    src = _bars()
    out = df_to_bars(bars_to_df(src), "USDJPY", "1h")
    assert len(out) == len(src)
    for a, b in zip(src, out):
        assert a.ts == b.ts and b.ts.tzinfo is not None
        assert (a.open, a.high, a.low, a.close) == (b.open, b.high, b.low, b.close)
        assert a.volume == b.volume        # volume を捨てない
        assert b.interval == "1h" and b.symbol == "USDJPY"


def test_resample_aggregates_ohlcv_correctly():
    src = _bars(n=8)                       # 1h × 8 → 4h × 2
    out = df_to_bars(resample(bars_to_df(src), "4h"), "USDJPY", "4h")
    assert len(out) == 2
    assert out[0].open == src[0].open      # first
    assert out[0].close == src[3].close    # last
    assert out[0].high == max(b.high for b in src[:4])
    assert out[0].low == min(b.low for b in src[:4])
    assert out[0].volume == sum(b.volume for b in src[:4])  # sum


def test_resample_bucket_boundary_is_utc_epoch_anchored():
    """バケット境界は UTC 固定。開始時刻がずれても境界は動かない。

    実運用とバックテストで同じ境界を使うことが目的。origin を既定
    (=データ先頭) にすると、取得開始時刻によって 4h の切り方が変わる。
    """
    shifted = _bars(n=8, start=NOW + timedelta(hours=2))  # 02:00 開始
    out = df_to_bars(resample(bars_to_df(shifted), "4h"), "USDJPY", "4h")
    assert out[0].ts == datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
    assert all(b.ts.hour % 4 == 0 for b in out)


def test_resample_drops_incomplete_buckets_only_when_empty():
    """データが無いバケットは落ちるが、部分的なバケットは残る。"""
    out = df_to_bars(resample(bars_to_df(_bars(n=6)), "4h"), "USDJPY", "4h")
    assert len(out) == 2                   # 00-04 完全 + 04-08 部分 (2 本)
```

- [ ] **Step 1b: `datafeed/bars.py` を実装**

`src/agentic_fx/datafeed/bars.py`:

```python
"""足の変換 (Bar ⇄ DataFrame、リサンプル)。

データ層に置く理由: PriceProvider がネイティブに無い足を導出するのに
必要であり、indicators (Task 4) もこれを import する。指標計算の付属物
ではなく、データ層の基本操作である。
"""
from __future__ import annotations

import pandas as pd

from agentic_fx.core.contracts import Bar

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum"}


def bars_to_df(bars: list[Bar]) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": [b.open for b in bars], "high": [b.high for b in bars],
         "low": [b.low for b in bars], "close": [b.close for b in bars],
         "volume": [b.volume for b in bars]},
        index=pd.DatetimeIndex([b.ts for b in bars], tz="UTC"))


def df_to_bars(df: pd.DataFrame, symbol: str, interval: str) -> list[Bar]:
    return [Bar(symbol, interval, ts.to_pydatetime(),
                float(r["open"]), float(r["high"]), float(r["low"]),
                float(r["close"]), float(r["volume"]))
            for ts, r in df.iterrows()]


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """OHLCV リサンプル。**バケット境界は UTC epoch 固定**。

    origin を既定 (データ先頭) にすると、取得開始時刻によって 4h の
    切り方が変わり、実運用とバックテストで違う足を見ることになる。
    """
    return df.resample(rule, origin="epoch").agg(_AGG).dropna()
```

- [ ] **Step 1b-2: config に `intervals` / `primary_intervals` を追加**

**`_Strict` は `extra="forbid"` なので、モデルと example は必ず同時に変更すること。** 片方だけ変えると既存の `load_settings(EXAMPLE)` を使う全テストが即座に落ちる。

`src/agentic_fx/config.py`:

```python
class DatafeedSettings(_Strict):
    yfinance: SourceToggle
    mt5: SourceToggle
    twelvedata: SourceToggle
    freshness_max_min: float = Field(gt=0)
    # 取引の時間軸は固定しない (設計書 §5)。扱う足と、判断が依存する足
    intervals: list[str] = Field(default_factory=lambda: ["1m", "1h"],
                                 min_length=1)
    primary_intervals: list[str] = Field(default_factory=lambda: ["1h"],
                                         min_length=1)

    @model_validator(mode="after")
    def _check_intervals(self):
        from agentic_fx.datafeed.sources import INTERVAL_MIN
        unknown = [i for i in self.intervals + self.primary_intervals
                   if i not in INTERVAL_MIN]
        if unknown:
            raise ValueError(f"unknown interval(s): {unknown}")
        if "1m" not in self.intervals:
            # ペーパー約定判定が 1 分足に依存する構造的要件
            raise ValueError("datafeed.intervals must include '1m'")
        missing = set(self.primary_intervals) - set(self.intervals)
        if missing:
            raise ValueError(
                f"primary_intervals must be a subset of intervals: {missing}")
        return self
```

**注意**: `INTERVAL_MIN` は関数内 import にする。モジュールトップで import すると `config` → `datafeed.sources` → (yfinance/httpx) の重い依存が設定読み込みに巻き込まれ、循環 import の温床にもなる。

`config/settings.yaml.example` の `datafeed:` ブロックに追記:

```yaml
datafeed:
  yfinance:   {enabled: true}
  mt5:        {enabled: false, bridge_url: "http://localhost:8812"}
  twelvedata: {enabled: false}
  freshness_max_min: 20
  intervals: [1m, 5m, 15m, 1h, 4h]   # 取得・保持する足 (1m は必須)
  primary_intervals: [1h]            # 判断 Mission が依存する足 (healthcheck の対象)
```

`tests/test_config.py` に追加:

```python
def _with_datafeed(tmp_path, **overrides):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["datafeed"].update(overrides)
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


def test_intervals_defaults_from_example():
    s = load_settings(EXAMPLE)
    assert "1m" in s.datafeed.intervals
    assert set(s.datafeed.primary_intervals) <= set(s.datafeed.intervals)


def test_intervals_must_include_1m(tmp_path):
    """1m はペーパー約定判定の構造的要件なので外せない。"""
    with pytest.raises(ConfigError, match="1m"):
        load_settings(_with_datafeed(tmp_path, intervals=["1h", "4h"],
                                     primary_intervals=["1h"]))


def test_primary_intervals_must_be_subset(tmp_path):
    with pytest.raises(ConfigError, match="subset"):
        load_settings(_with_datafeed(tmp_path, intervals=["1m", "1h"],
                                     primary_intervals=["4h"]))


def test_unknown_interval_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown interval"):
        load_settings(_with_datafeed(tmp_path, intervals=["1m", "3h"],
                                     primary_intervals=["1m"]))


def test_empty_primary_intervals_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(_with_datafeed(tmp_path, primary_intervals=[]))
```

- [ ] **Step 1c: PriceProvider の失敗するテストを書く**

`tests/datafeed/test_price_provider.py`:

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar, FixedClock, Quote
from agentic_fx.datafeed import sources
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


def _fresh_bars(pair="USDJPY", n=30, interval="1m"):
    """直近 n 本の健全なバー。interval を変えると足の幅も追従する。"""
    step = timedelta(minutes=sources.INTERVAL_MIN[interval])
    start = NOW - step * n
    return [Bar(pair, interval, start + step * i,
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


def test_td_quote_used_when_mt5_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("TWELVEDATA_API_KEY", "k")
    s = load_settings(EXAMPLE).model_copy(deep=True)
    s.datafeed.mt5.enabled = True
    s.datafeed.twelvedata.enabled = True
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    p = PriceProvider(conn, s, FixedClock(NOW))
    tdq = Quote("USDJPY", 148.49, 148.51, NOW, "twelvedata")
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_quote",
               side_effect=OSError("down")), \
         patch("agentic_fx.datafeed.price_provider.sources.td_quote",
               return_value=tdq):
        assert p.get_quote("USDJPY").source == "twelvedata"


def test_yfinance_disabled_is_respected(tmp_path):
    s = load_settings(EXAMPLE).model_copy(deep=True)
    s.datafeed.yfinance.enabled = False
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    p = PriceProvider(conn, s, FixedClock(NOW))
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote") as yq:
        with pytest.raises(DataUnhealthy):
            p.get_quote("USDJPY")
        yq.assert_not_called()


def test_get_bars_derives_4h_from_1h_on_yfinance(tmp_path):
    """yfinance は 4h をネイティブに持たないので 1h から導出する。

    足を固定しない方針のため、4h の要求を拒否せず導出で満たす。
    由来を記録して「ネイティブ 4h」と区別できることも併せて確認する。
    """
    _, p = _provider(tmp_path)   # yfinance のみ enabled
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars(interval="1h", n=100)) as yb:
        bars = p.get_bars("USDJPY", "4h")
    yb.assert_called_once()
    assert yb.call_args.args[1] == "1h"          # 1h を取りに行っている
    assert all(b.interval == "4h" for b in bars)  # 返るのは 4h
    assert "derived" in p.bars_origin("USDJPY", "4h")


def test_get_bars_uses_native_4h_when_source_has_it(tmp_path):
    """MT5 は 4h をネイティブに持つので resample しない。"""
    _, p = _provider(tmp_path, mt5=True)
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_bars",
               return_value=_fresh_bars(interval="4h", n=30)) as mb:
        p.get_bars("USDJPY", "4h")
    assert mb.call_args.args[2] == "4h"           # 4h をそのまま要求
    assert p.bars_origin("USDJPY", "4h") == "mt5"  # derived が付かない


def test_get_bars_rejects_unknown_interval(tmp_path):
    _, p = _provider(tmp_path)
    with pytest.raises(ValueError, match="unknown interval"):
        p.get_bars("USDJPY", "3h")


def test_last_bars_source_tracks_quality_flag(tmp_path):
    conn, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars()):
        p.get_bars("USDJPY", "1m", 1)
    assert p.last_bars_source("USDJPY", "1m") == "yfinance"
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("down")):
        p.get_bars("USDJPY", "1m", 1)  # キャッシュフォールバック
    assert p.last_bars_source("USDJPY", "1m") == "cache"


def test_healthcheck_requires_bars_too(tmp_path):
    _, p = _provider(tmp_path)
    q = Quote("USDJPY", 148.5, 148.5, NOW, "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=q), \
         patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("down")):
        with pytest.raises(DataUnhealthy):
            p.healthcheck("USDJPY")  # quote 健全でも bars 全滅なら fail closed
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
from agentic_fx.datafeed.bars import bars_to_df, df_to_bars, resample
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
        self._bars_source: dict[tuple[str, str], str] = {}
        self._bars_origin: dict[tuple[str, str], str] = {}

    def _td_key(self) -> str | None:
        if not self.settings.datafeed.twelvedata.enabled:
            return None
        return os.environ.get("TWELVEDATA_API_KEY")

    # ---- quote ----------------------------------------------------------

    def get_quote(self, pair: str) -> Quote:
        errors: list[str] = []
        for name, fn in self._chain(pair, kind="quote"):
            try:
                q = fn()
                validate_quote(q, self.clock.now(),
                               self.settings.datafeed.freshness_max_min)
                return q
            except Exception as e:  # noqa: BLE001 — 次ソースへ
                _log.warning("quote source %s failed for %s: %s", name, pair, e)
                errors.append(f"{name}: {e}")
        raise DataUnhealthy(f"all quote sources failed for {pair}: {errors}")

    def _chain(self, pair: str, *, kind: str, interval: str = "1m",
               lookback_days: int = 5):
        """優先順位 MT5 → TD → yfinance。enabled なソースのみ (設計書 §5)。"""
        d = self.settings.datafeed
        td_key = self._td_key()
        chain = []
        if d.mt5.enabled:
            chain.append(("mt5", (
                lambda: sources.mt5_quote(d.mt5.bridge_url, pair))
                if kind == "quote" else
                (lambda: sources.mt5_bars(d.mt5.bridge_url, pair, interval,
                                          lookback_days))))
        if td_key:
            chain.append(("twelvedata", (
                lambda: sources.td_quote(td_key, pair))
                if kind == "quote" else
                (lambda: sources.td_bars(td_key, pair, interval,
                                         lookback_days))))
        if d.yfinance.enabled:
            chain.append(("yfinance", (
                lambda: sources.yf_quote(pair))
                if kind == "quote" else
                (lambda: sources.yf_bars(pair, interval, lookback_days))))
        return chain

    # ---- bars -----------------------------------------------------------

    def get_bars(self, pair: str, interval: str,
                 lookback_days: int = 5) -> list[Bar]:
        # 足は決め打ちしない。ソースがネイティブに持つなら素直に要求し、
        # 無ければより細かいネイティブ足から resample で導出する。
        if interval not in sources.INTERVAL_MIN:
            raise ValueError(f"unknown interval: {interval}")
        d = self.settings.datafeed
        now = self.clock.now()
        interval_min = sources.INTERVAL_MIN[interval]
        errors: list[str] = []
        for name, fn in self._chain(pair, kind="bars", interval=interval,
                                    lookback_days=lookback_days):
            try:
                if interval in sources.NATIVE_INTERVALS[name]:
                    bars, origin = fn(), name
                else:
                    base = self._finest_native_base(name, interval)
                    bars = self._derive(pair, name, base, interval,
                                        lookback_days)
                    origin = f"{name}({base}→{interval} derived)"
                validate_bars(bars, now, d.freshness_max_min, interval_min)
                ohlcv.upsert_bars(self.conn, bars)
                # source は素の名前、origin は導出情報つき (別々に持つ —
                # 品質フラグの完全一致判定を導出で壊さないため)
                self._bars_source[(pair, interval)] = name
                self._bars_origin[(pair, interval)] = origin
                return bars
            except Exception as e:  # noqa: BLE001
                _log.warning("bars source %s failed for %s: %s", name, pair, e)
                errors.append(f"{name}: {e}")

        cached = ohlcv.load_bars(self.conn, pair, interval)
        if cached:
            try:
                validate_bars(cached, now, d.freshness_max_min, interval_min)
                _log.warning("using cached bars for %s %s", pair, interval)
                self._bars_source[(pair, interval)] = "cache"
                self._bars_origin[(pair, interval)] = "cache"
                return cached
            except DataUnhealthy as e:
                errors.append(f"cache: {e}")
        raise DataUnhealthy(f"all bar sources failed for {pair}: {errors}")

    def last_bars_source(self, pair: str, interval: str) -> str | None:
        """素の source 名 ("mt5" / "twelvedata" / "yfinance" / "cache")。

        データ品質フラグの判定に使うため、導出の有無で値が変わらないこと
        (`== "yfinance"` の完全一致判定が導出時に外れると品質フラグを
        見落とす)。導出情報は bars_origin 側に持つ。
        """
        return self._bars_source.get((pair, interval))

    def bars_origin(self, pair: str, interval: str) -> str | None:
        """由来 ("mt5" / "yfinance(1h→4h derived)" / "cache" 等)。

        ネイティブ足か導出足かを区別する。status 表示と、実運用と
        バックテストで同じ足を見ているかの確認に使う。
        """
        return self._bars_origin.get((pair, interval))

    # ---- 足の導出 ---------------------------------------------------------

    def _fetch_native(self, pair: str, source: str, interval: str,
                      lookback_days: float) -> list[Bar]:
        """指定 source から指定のネイティブ足を取る (_chain を経由しない)。

        _chain の closure は 1 つの interval に束縛されるため、導出時に
        別の足を取りに行けない。導出専用にここで直接ディスパッチする。
        """
        d = self.settings.datafeed
        days = max(1, int(lookback_days))
        if source == "mt5":
            return sources.mt5_bars(d.mt5.bridge_url, pair, interval, days)
        if source == "twelvedata":
            return sources.td_bars(self._td_key(), pair, interval, days)
        if source == "yfinance":
            return sources.yf_bars(pair, interval, days)
        raise ValueError(f"unknown source: {source}")

    def _finest_native_base(self, source: str, interval: str) -> str:
        """interval を導出できる、最も粗いネイティブ足を選ぶ。

        粗いほど取得本数が少なく API 負荷が低い。interval_min の約数で
        なければ境界が合わないため候補から外す (例: 4h を 15m から作るのは
        可、90m からは不可)。
        """
        want = sources.INTERVAL_MIN[interval]
        cands = [i for i in sources.NATIVE_INTERVALS[source]
                 if sources.INTERVAL_MIN[i] < want
                 and want % sources.INTERVAL_MIN[i] == 0]
        if not cands:
            raise DataUnhealthy(
                f"{source} cannot provide or derive {interval}")
        return max(cands, key=lambda i: sources.INTERVAL_MIN[i])

    def _derive(self, pair: str, source: str, base: str, interval: str,
                lookback_days: int) -> list[Bar]:
        """base 足を取って interval へ resample する。

        バケット境界は `bars.resample` が UTC epoch に固定する。実運用と
        バックテストで同じ境界を使うことが目的。なお**ネイティブ足
        (MT5 の H4 等) とは境界が一致しないことがある** (ブローカーの
        サーバ時刻基準のため) — だから由来を記録する。
        """
        # 粗い足 N 本を作るには細かい足が N×(比) 本要る。取得期間を
        # 同じにすると本数が足りず、指標計算に必要な長さを満たさない
        ratio = sources.INTERVAL_MIN[interval] / sources.INTERVAL_MIN[base]
        raw = self._fetch_native(pair, source, base, lookback_days * ratio)
        return df_to_bars(resample(bars_to_df(raw), interval), pair, interval)

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
        """quote と primary_intervals の全バーを検証する。

        足は 1h 決め打ちにしない (取引の時間軸を固定しない方針)。判断が
        依存する足はすべて健全でなければ Mission を回さない (fail closed)。
        """
        source = self.get_quote(pair).source
        for interval in self.settings.datafeed.primary_intervals:
            self.get_bars(pair, interval)   # DataUnhealthy はそのまま伝播
        return source
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
- Consumes: `datafeed/bars.py` の `bars_to_df` / `resample` (**Task 3 で作成済み** — 足の変換はデータ層の責務であり、PriceProvider がネイティブに無い足を導出するのに先に必要になる。indicators では import して使うだけで再定義しない)
- Produces:
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

# 足の変換は datafeed/bars.py (Task 3) に置く。ここで再定義しない
from agentic_fx.datafeed.bars import bars_to_df, resample  # noqa: F401


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
  - `__init__(self, data_dir: Path, embedding_function=None)` — `chromadb.PersistentClient(path=data_dir)`、コレクション `news` / `reflections`。`embedding_function=None` なら chromadb デフォルト (all-MiniLM-L6-v2、**初回にモデルを自動 DL する — オフライン環境では init 時に事前 DL するか注入が必要**)。**テストは決定論的な fake embedding を注入し、ネットワークに依存しない**
  - 本番の初期化失敗 (モデル未配置 + オフライン) は Rag 生成時の例外としてサービス起動失敗で顕在化させる (黙って劣化しない)
  - `add_news(self, articles: list[dict], now: datetime) -> int` — dict keys: url/title/body/source_name/published。**url をIDにして重複 upsert**。metadata に `added_at` (ISO)。追加件数を返す
  - `search_news(self, query: str, n: int = 5) -> list[dict]` — `{url, title, body, source_name}` のリスト
  - `cleanup_news(self, now: datetime, hours: int = 48) -> int` — `added_at` が閾値より古いものを削除、削除件数を返す (設計書 §12)
  - `add_reflection(self, order_id: int, content: str, pair: str) -> None` / `search_reflections(self, query: str, n: int = 5) -> list[dict]`

- [ ] **Step 1: 失敗するテストを書く**

`tests/store/test_rag.py`:

```python
import hashlib

from datetime import datetime, timedelta, timezone

from agentic_fx.store.rag import Rag

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


class FakeEmbedding:
    """決定論的 fake embedding (ネットワーク・モデル DL 不要)。"""

    def __call__(self, input):  # noqa: A002 — chromadb の EF 規約
        out = []
        for text in input:
            h = hashlib.sha256(text.encode()).digest()
            out.append([b / 255.0 for b in h[:16]])
        return out

    def name(self):
        return "fake"


def _rag(tmp_path):
    return Rag(tmp_path / "rag", embedding_function=FakeEmbedding())


ARTS = [
    {"url": "https://ex.com/a1", "title": "Dollar rallies on CPI",
     "body": "The US dollar strengthened after CPI data.",
     "source_name": "ex", "published": None},
    {"url": "https://ex.com/a2", "title": "BOJ keeps rates",
     "body": "Bank of Japan kept interest rates unchanged.",
     "source_name": "ex", "published": None},
]


def test_add_and_search(tmp_path):
    rag = _rag(tmp_path)
    assert rag.add_news(ARTS, NOW) == 2
    hits = rag.search_news("US dollar CPI", n=1)
    assert len(hits) == 1
    assert hits[0]["url"] in {"https://ex.com/a1", "https://ex.com/a2"}


def test_upsert_dedup_by_url(tmp_path):
    rag = _rag(tmp_path)
    rag.add_news(ARTS, NOW)
    rag.add_news(ARTS, NOW)  # 再投入
    assert rag.count_news() == 2


def test_cleanup_removes_old(tmp_path):
    rag = _rag(tmp_path)
    rag.add_news([ARTS[0]], NOW - timedelta(hours=50))
    rag.add_news([ARTS[1]], NOW)
    removed = rag.cleanup_news(NOW, hours=48)
    assert removed == 1
    assert rag.count_news() == 1


def test_reflections_roundtrip(tmp_path):
    rag = _rag(tmp_path)
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
    def __init__(self, data_dir: Path, embedding_function=None) -> None:
        self._client = chromadb.PersistentClient(path=str(data_dir))
        kwargs = {}
        if embedding_function is not None:
            kwargs["embedding_function"] = embedding_function
        self._news = self._client.get_or_create_collection("news", **kwargs)
        self._refl = self._client.get_or_create_collection("reflections",
                                                           **kwargs)

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
Expected: PASS (fake embedding のためネットワーク・モデル DL 不要)

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
    from tests.store.test_rag import FakeEmbedding
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())
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
     "date": "2026-07-24T02:00:00-04:00", "impact": "Medium",
     "forecast": "", "previous": "0.2%"},  # 7/24 06:00 UTC = 24h 圏外
]


def test_fetch_maps_fields_and_normalizes_utc():
    resp = MagicMock()
    resp.json.return_value = FF_JSON
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp):
        events = fetch_ff_calendar()
    assert events[0]["country"] == "USD"
    assert events[0]["importance"] == 3
    assert events[1]["importance"] == 2
    # ts は UTC へ正規化して保存する (ISO 文字列比較の順序保証のため)
    assert events[0]["ts"].tzinfo == timezone.utc
    assert events[0]["ts"].hour == 19  # 15:30-04:00 → 19:30 UTC


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
    assert len(rows) == 1  # USD CPI (22 日 19:30 UTC) のみ。GBP は 42h 後で圏外
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
from datetime import datetime, timezone

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
        # UTC へ正規化 (store は ISO 文字列比較のため offset 混在を許さない)
        ts = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
        out.append({"ts": ts,
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

---

## 追記 (2026-07-26): 通貨換算とクロス通貨ペアの解禁

設計書 改訂第 12 版で「口座通貨と換算」が明文化されたことに伴う、本プランへの追加要件。

### 背景
プラン 2 の実装中に**通貨単位の不整合**が見つかった。`loss_per_lot` はクォート通貨建て、
`risk_amount` は口座通貨建てで、両者が異なるペア (JPY 口座での EURUSD 等) では
**リスクを 150 倍、レバレッジを 20 倍過小評価**していた。換算レートの供給元が無かったため、
暫定措置として `compute_size` がクォート通貨 ≠ 口座通貨のペアを `SizingError` で拒否している
(config の `pairs` も USDJPY のみに縮小済み)。

**本プランで `quote_to_account_rate` を実装することが、この制限を解除する条件**である。

### Task 3 への追加 (上記 Interfaces 参照)
`PriceProvider.quote_to_account_rate(quote_ccy, account_ccy) -> float` を実装する。
直接ペア → 逆ペア → USD 経由クロス の順に解決し、いずれも不可なら `DataUnhealthy`。

**テストで検証すること:**
- 同一通貨は `1.0` (レート取得を試みないこと)
- 直接ペア (quote=USD, account=JPY → USDJPY の価格)
- 逆ペア (quote=JPY, account=USD → USDJPY の逆数)。**逆数が正しいこと**を実測で検証
- USD 経由クロス (quote=EUR, account=JPY のような直接・逆ペアとも無い場合)
- レート用 quote が陳腐・不健全なら `DataUnhealthy` (握りつぶさない)
- `VENDOR_SYMBOLS` 未登録の通貨対で `DataUnhealthy`

### 本プラン完了後に別途行うこと (このプランのスコープ外)
1. `core/sizing.py` の暫定 fail closed を解除し、`quote_to_account_rate` の値を受け取って
   `loss_per_lot` を口座通貨に換算する (`compute_size` に換算レート引数を追加)
2. `core/risk_gate.py` の `GateContext` に換算レートを追加し、executor が PriceProvider から供給
3. `config/settings.yaml.example` の `pairs` に EURUSD を戻す
4. **非退行の確認**: USDJPY の数量が従来と一致すること (day 0.09 / swing 0.04)。
   換算レート 1.0 の経路で計算結果が変わらないことを実測すること
