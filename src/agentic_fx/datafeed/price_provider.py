"""PriceProvider — ソース解決 (MT5→TD→yfinance)・健全性検証・SQLite キャッシュ。設計書 §5。"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
from collections.abc import Callable

from agentic_fx.config import Settings
from agentic_fx.core.contracts import Bar, Clock, InstrumentSpec, Quote
from agentic_fx.datafeed import sources
from agentic_fx.datafeed.bars import bars_to_df, df_to_bars, pandas_rule, resample
from agentic_fx.datafeed.health import (
    DataUnhealthy, validate_bars, validate_quote,
)
from agentic_fx.store import ohlcv

_log = logging.getLogger("agentic_fx.price")

# Phase 1 は組み込みテーブル。Phase 3 で MT5 の symbol_info 照会に置換する
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
        key = os.environ.get("TWELVEDATA_API_KEY")
        if not key:
            # 呼んでも必ず失敗するソースを chain に入れない (毎回 warning を
            # 吐くだけになる)。設定ミスに気づけるよう 1 行だけ残す
            _log.warning("twelvedata is enabled but TWELVEDATA_API_KEY is unset")
        return key

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
                # 例外の型で絞らない。sources は naive datetime を ValueError で
                # 弾く (fail closed) ため、DataUnhealthy/HTTPError だけを捕まえる
                # と ValueError が漏れて get_quote ごと落ちる (フォールバックの
                # はずが全滅する)
                _log.warning("quote source %s failed for %s: %s", name, pair, e)
                errors.append(f"{name}: {e}")
        raise DataUnhealthy(f"all quote sources failed for {pair}: {errors}")

    def _chain(self, pair: str, *, kind: str, interval: str = "1m",
               lookback_days: int = 5) -> list[tuple[str, Callable[[], object]]]:
        """優先順位 MT5 → TD → yfinance。enabled なソースのみ (設計書 §5)。"""
        d = self.settings.datafeed
        td_key = self._td_key()
        chain: list[tuple[str, Callable[[], object]]] = []
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
            except Exception as e:  # noqa: BLE001 — get_quote と同じ理由で広く捕る
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
            except Exception as e:  # noqa: BLE001
                # キャッシュも健全性検証を通さない限り使わない (fail closed)
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
        # lookback_days は「期間」なので比を掛けなくても期間自体は満たせるが、
        # 粗い足に畳むと本数が 1/比 になり、指標計算に必要な長さを満たさない
        # (5 日分の 4h は 30 本しかない)。取得期間に比の余裕を持たせる。
        ratio = sources.INTERVAL_MIN[interval] / sources.INTERVAL_MIN[base]
        raw = self._fetch_native(pair, source, base, lookback_days * ratio)
        # interval 文字列は pandas の freq alias ではない (pandas_rule で写す)
        return df_to_bars(resample(bars_to_df(raw), pandas_rule(interval)),
                          pair, interval)

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

    # ---- 口座通貨への換算 --------------------------------------------------

    def _rate_symbol(self, base: str, quote: str) -> tuple[str, bool] | None:
        """base 1 単位 = quote いくら、を得る論理シンボルと反転要否を返す。

        VENDOR_SYMBOLS に無ければ None (= 構造的に取得不可)。**「不健全」と
        「未登録」を区別するためにここで取得はしない** — 未登録なら別経路
        (USD 経由) を試せるが、取得したレートが陳腐だった場合に別経路へ
        逃がすと fail closed が骨抜きになる。
        """
        if f"{base}{quote}" in sources.VENDOR_SYMBOLS:
            return f"{base}{quote}", False
        if f"{quote}{base}" in sources.VENDOR_SYMBOLS:
            return f"{quote}{base}", True
        return None

    def _rate_of(self, spec: tuple[str, bool]) -> float:
        """シンボルの mid を取り、必要なら反転する。quote は健全性検証済み。

        mid (bid/ask の中値) を使う: 換算はサイジングの尺度であって約定価格
        ではないため、方向 (買い/売り) に依存しない値を使う。
        """
        symbol, invert = spec
        q = self.get_quote(symbol)      # 鮮度・有限性・正値の検証を通る
        mid = (q.bid + q.ask) / 2
        if not math.isfinite(mid) or mid <= 0:
            raise DataUnhealthy(f"invalid mid price for {symbol}: {mid}")
        return 1.0 / mid if invert else mid

    def quote_to_account_rate(self, quote_ccy: str, account_ccy: str) -> float:
        """クォート通貨 1 単位 = 口座通貨いくらか (設計書 §5「口座通貨と換算」)。

        ①直接ペア → ②逆ペア (逆数) → ③USD 経由のクロス の順に解決する。
        いずれも不可なら DataUnhealthy。呼び出し側 (sizing) はこれを
        SizingError に変換して fail closed する — 古いレートや推測値での
        サイジングは無音の過大建玉になるため、ここで握りつぶさない。
        """
        if quote_ccy == account_ccy:
            return 1.0
        direct = self._rate_symbol(quote_ccy, account_ccy)
        if direct is not None:
            return self._rate_of(direct)

        # ③ USD 経由のクロス (例: EUR→JPY = EURUSD × USDJPY)
        legs: list[tuple[str, bool]] = []
        for base, quote in ((quote_ccy, "USD"), ("USD", account_ccy)):
            if base == quote:
                continue                # 片脚が USD 同士なら換算不要
            spec = self._rate_symbol(base, quote)
            if spec is None:
                raise DataUnhealthy(
                    f"cannot convert {quote_ccy}->{account_ccy}: no symbol for "
                    f"{base}/{quote} in VENDOR_SYMBOLS")
            legs.append(spec)
        if not legs:
            # quote_ccy == account_ccy は先に返しているため到達しない
            raise DataUnhealthy(
                f"cannot convert {quote_ccy}->{account_ccy}: no route")
        rate = 1.0
        for spec in legs:
            rate *= self._rate_of(spec)
        return rate
