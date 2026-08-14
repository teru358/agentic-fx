"""PriceProvider — ソース解決 (MT5→TD→yfinance)・健全性検証・SQLite キャッシュ。設計書 §5。"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from agentic_fx._safe_error import safe_error_text as _safe_error_text
from agentic_fx.config import Settings
from agentic_fx.core.contracts import (
    Bar, Clock, ConversionRate, InstrumentSpec, Quote,
)
from agentic_fx.datafeed import sources
from agentic_fx.datafeed.bars import bars_to_df, df_to_bars, pandas_rule, resample
from agentic_fx.datafeed.health import (
    DataUnhealthy, validate_bars, validate_conversion_skew, validate_quote,
)
from agentic_fx.store import ohlcv

_log = logging.getLogger("agentic_fx.price")

# 境界がソース依存の足。MT5 は H4/D1 をブローカーのサーバ時刻基準で集計するため
# (実測 2026-07-28、稼働中の bridge に直接問い合わせ: サーバ UTC+3、H4 は真 UTC の
# 21/01/05/09/13/17 時、D1 は 21:00 = NY クローズ 17:00 EDT)、ネイティブに取ると
# ソースごとに違う格子の足が同じ interval の系列に混ざる。1h から epoch 導出した
# 4h (00/04/08/12/16/20 時) とは 1 本も時刻を共有しない。ohlcv の主キーは
# (symbol, interval, bar_time) で由来を区別できないため、混ざると時間的に重複した
# 2 つの格子が 1 本の系列として返り、validate_bars も「足が多すぎる」方向は見ない。
# 常に細かい足から導出し、システム内の格子を 1 つに保つ。
DERIVE_ONLY_INTERVALS = frozenset({"4h", "1d"})

# F1 (fix round 1, codex Critical): 永続化用 source ID とチェーン表示名の
# マッピング。live MT5 は ohlcv に "mt5-live" として保存する
# (spec §6: live="mt5-live" / 一括インポータ="mt5" — 混在させると、Task 5 の
# 一括履歴インポート (既存行不変・import_history_bars) を Phase 3 の live 上書き
# (upsert_cache_bars) が破壊する PK 衝突になる)。`_chain`/`last_bars_source`/
# `bars_origin` はチェーンの表示名 ("mt5") をそのまま使い続ける — 変えるのは
# 永続化境界 (upsert_cache_bars/load_cache_bars の source 引数) だけ。
_STORAGE_SOURCE = {"mt5": "mt5-live"}


def _storage_source(chain_name: str) -> str:
    """チェーン表示名 (`_chain` の name) → ohlcv 永続化用 source ID。"""
    return _STORAGE_SOURCE.get(chain_name, chain_name)

# 秘密抑止 (_safe_error_text) は datafeed/_safe_error.py に一本化してある
# (Task 8: 同じ関数が price_provider / news_collector に複製され、3 つ目が
# 生じる時点で複製方針が割に合わなくなったため)。挙動は従前と同一。


# Phase 1 は組み込みテーブル。Phase 3 で MT5 の symbol_info 照会に置換する。
# max_lot: 2026-07-29 OANDA-Japan MT5 実測で静的値 50.0 は実値 10.0 と不一致
# だったため 10.0 に修正 (ブリーフ実測)。base_currency/quote_currency は
# symbol の切り出しではなくここで明示する (設計書 §5)。
_SPECS = {
    "USDJPY": InstrumentSpec("USDJPY", 0.01, 0.01, 10.0, 0.01, 100_000,
                             base_currency="USD", quote_currency="JPY"),
    "EURUSD": InstrumentSpec("EURUSD", 0.0001, 0.01, 10.0, 0.01, 100_000,
                             base_currency="EUR", quote_currency="USD"),
}


class PriceProvider:
    def __init__(self, conn: sqlite3.Connection, settings: Settings,
                 clock: Clock, readonly: bool = False) -> None:
        self.conn = conn
        self.settings = settings
        self.clock = clock
        # CR-4 (裁定書 F-5): 子プロセス (mission_worker.py) は conn に
        # db.connect_readonly (mode=ro) を渡す。get_bars/_derive の
        # cache 書込 (ohlcv.upsert_cache_bars) は RO 接続下で
        # sqlite3.OperationalError になるため、readonly=True のときは
        # 書込呼び出し自体をスキップする (RPC 経由の親委譲はしない —
        # RPC 面を拡大しない設計裁定)。
        self.readonly = readonly
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
                text = _safe_error_text(e)
                _log.warning("quote source %s failed for %s: %s", name, pair, text)
                errors.append(f"{name}: {text}")
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
        # 無ければより細かいネイティブ足から resample で導出する。ただし
        # DERIVE_ONLY_INTERVALS はネイティブに持っていても必ず導出する
        # (境界がソース依存のため。定数のコメント参照)。
        if interval not in sources.INTERVAL_MIN:
            raise ValueError(f"unknown interval: {interval}")
        d = self.settings.datafeed
        now = self.clock.now()
        interval_min = sources.INTERVAL_MIN[interval]
        errors: list[str] = []
        for name, fn in self._chain(pair, kind="bars", interval=interval,
                                    lookback_days=lookback_days):
            try:
                if (interval in sources.NATIVE_INTERVALS[name]
                        and interval not in DERIVE_ONLY_INTERVALS):
                    bars, origin = fn(), name
                    validate_bars(bars, now, d.freshness_max_min, interval_min)
                    # 境界がソース非依存の足だけを保存する。source にはチェーンの
                    # 実ソース名を渡す (全部 yfinance 名義で書くと live ソース同士
                    # が上書きし合い、source 列が嘘になる — レビュー裁定 codex I7)。
                    # 永続化用 ID への変換は _storage_source (F1)。
                    if not self.readonly:
                        ohlcv.upsert_cache_bars(self.conn, bars,
                                          source=_storage_source(name))
                else:
                    base = self._finest_native_base(name, interval)
                    # 保存は _derive が base 足に対して行う (導出足は保存しない)
                    bars = self._derive(pair, name, base, interval,
                                        lookback_days)
                    origin = f"{name}({base}→{interval} derived)"
                    validate_bars(bars, now, d.freshness_max_min, interval_min)
                # source は素の名前、origin は導出情報つき (別々に持つ —
                # 品質フラグの完全一致判定を導出で壊さないため)
                self._bars_source[(pair, interval)] = name
                self._bars_origin[(pair, interval)] = origin
                return bars
            except Exception as e:  # noqa: BLE001 — get_quote と同じ理由で広く捕る
                text = _safe_error_text(e)
                _log.warning("bars source %s failed for %s: %s", name, pair, text)
                errors.append(f"{name}: {text}")

        got = self._cached_bars(pair, interval, now, errors, lookback_days)
        if got is not None:
            bars, origin = got
            _log.warning("using cached bars for %s %s (%s)", pair, interval,
                         origin)
            self._bars_source[(pair, interval)] = "cache"
            self._bars_origin[(pair, interval)] = origin
            return bars
        raise DataUnhealthy(f"all bar sources failed for {pair}: {errors}")

    def _cached_bars(self, pair: str, interval: str, now: datetime,
                     errors: list[str], lookback_days: int
                     ) -> tuple[list[Bar], str] | None:
        """キャッシュから interval の足を作る。健全性検証を通らなければ None。

        **要求された足そのもの → より細かい base 足、の順に試す**。
        導出足は保存しない設計 (DERIVE_ONLY_INTERVALS のコメント参照) なので、
        導出で得ていた足 (30m 等) はキャッシュにも存在しない。base へ流れないと
        「ソース健在時は導出で返るのに、全滅時だけ落ちる」ことになる。

        候補から DERIVE_ONLY_INTERVALS を除くのは `_base_candidates` と同じ規則。
        **要求された足そのものにも適用する** — 4h/1d の行は本来存在しないが、
        本修正より前のバイナリが書いた残骸があり得る。それはブローカー格子の
        足なので、読むと I-2 で塞いだ「格子の混在」が静かに戻る。

        source 列は「どの source が書いたか」を区別する PK の一部になった
        (ohlcv v2)。ここは「どの source が書いたか」を知らないサイトなので、
        **`_chain` が返す live source 名を優先順に 1 つずつ試し**、各名で単一
        source の `load_cache_bars(..., source=...)` を読む (永続化 ID への変換は
        `_storage_source`、F1) — 複数 source の行を 1 回のクエリで混ぜて
        返さない (上書き 1)。Phase 1 の実態は yfinance のみなので挙動は不変。

        **F6 (fix round 1, codex I4 + sonnet Important-3): ループ順は
        source を外側・interval 候補を内側**にする (優先順位 MT5→TD→
        yfinance を維持したまま)。interval を外側にすると、優先度の低い
        source の「要求された足そのもの」が、優先度の高い source の
        「より粗い base 足からの導出」より先に採用されてしまい、
        ソース優先順位が崩れる。

        `lookback_days` は必須引数 (既定値で誤魔化さない — spec ③: helper
        側で推測すると `latest_1m_bar` の lookback_days=1 と通常呼び出しの
        既定 5 を区別できなくなる)。

        本 task (プラン 9 Task 9) 時点では `since` の計算は素朴な
        `now - timedelta(days=lookback_days)` — native/derive の区別・
        floor 適用は Task 10 が実装する。配線そのものが正しいことを
        独立に検査するための中間状態。
        """
        d = self.settings.datafeed
        since = now - timedelta(days=lookback_days)
        candidates = [i for i in [interval, *self._base_candidates(interval)]
                      if i not in DERIVE_ONLY_INTERVALS]
        live_sources = [name for name, _ in
                        self._chain(pair, kind="bars", interval=interval)]
        for name in live_sources:
            storage_name = _storage_source(name)
            for src in candidates:
                cached = ohlcv.load_cache_bars(self.conn, pair, src,
                                               source=storage_name,
                                               since=since)
                if not cached:
                    continue
                label = ("cache" if src == interval else f"cache({src})")
                label = f"{label}[{storage_name}]"
                try:
                    # キャッシュも健全性検証を通さない限り使わない (fail closed)
                    validate_bars(cached, now, d.freshness_max_min,
                                  sources.INTERVAL_MIN[src])
                    if src == interval:
                        return cached, "cache"
                    derived = self._resample(cached, pair, interval)
                    validate_bars(derived, now, d.freshness_max_min,
                                  sources.INTERVAL_MIN[interval])
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{label}: {_safe_error_text(e)}")
                    continue
                return derived, f"cache({src}→{interval} derived)"
        return None

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

    def _base_candidates(self, interval: str) -> list[str]:
        """interval を導出できる base 足を、粗い順に返す (ソース非依存)。

        粗いほど取得本数が少なく API 負荷が低い。interval_min の約数で
        なければ境界が合わないため候補から外す (例: 4h を 15m から作るのは
        可、90m からは不可)。**DERIVE_ONLY_INTERVALS は base にしない** —
        1d を「ネイティブ 4h から導出」にすると、ブローカー格子が裏口から
        入ってくる (1d は 1h から導出されること)。
        """
        want = sources.INTERVAL_MIN[interval]
        cands = [i for i, m in sources.INTERVAL_MIN.items()
                 if i not in DERIVE_ONLY_INTERVALS and m < want
                 and want % m == 0]
        return sorted(cands, key=lambda i: sources.INTERVAL_MIN[i],
                      reverse=True)

    def _finest_native_base(self, source: str, interval: str) -> str:
        """interval を導出できる、最も粗いネイティブ足を選ぶ。

        (関数名は `_finest` だが実際に選ぶのは「最も粗い」足。ブリーフの
        逐語コード由来の命名で、挙動は docstring のとおり。)
        """
        native = sources.NATIVE_INTERVALS[source]
        for base in self._base_candidates(interval):
            if base in native:
                return base
        raise DataUnhealthy(f"{source} cannot provide or derive {interval}")

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
        # **base 足の段階で健全性を検査する** (導出後の検査だけでは穴が開く)。
        # 検査後に **base 足を base の interval で保存する**。導出足は保存しない
        # (DERIVE_ONLY_INTERVALS のコメント参照: 格子の違う足が同じ系列に
        # 混ざるのを構造的に防ぐ。錨を変えてもキャッシュ破棄が要らなくなる)。
        # resample は base の部分欠損をバケット内に吸収してしまう: 4h バケット内の
        # 1h 4 本のうち 3 本が欠けても 1 本残ればバケットは生き残り、導出足は
        # 連続に見えて validate_bars を素通りする。base の粒度で検査すると
        # _MAX_GAP_BARS が「1h 欠損 3 本まで」という自然な意味になり、
        # 導出足で検査するより厳しくなる — それが意図 (fail closed)。
        # ここで送出される DataUnhealthy は get_bars のソース毎 except に
        # 捕まり、技術ログ warning + 次ソース (最終的にキャッシュ) へ流れる。
        validate_bars(raw, self.clock.now(),
                      self.settings.datafeed.freshness_max_min,
                      sources.INTERVAL_MIN[base])
        if not self.readonly:
            ohlcv.upsert_cache_bars(self.conn, raw, source=_storage_source(source))
        return self._resample(raw, pair, interval)

    def _resample(self, base_bars: list[Bar], pair: str,
                  interval: str) -> list[Bar]:
        """base 足 → interval 足。錨は `bars.BAR_ANCHOR` (既定 UTC epoch)。"""
        # interval 文字列は pandas の freq alias ではない (pandas_rule で写す)
        return df_to_bars(
            resample(bars_to_df(base_bars), pandas_rule(interval)),
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

    def _rate_of(self, spec: tuple[str, bool]) -> tuple[float, datetime]:
        """シンボルの**保守側**価格を取り、必要なら反転する。quote は健全性
        検証済み。戻り値は (rate, その脚の quote 観測時刻)。

        保守側 (設計書 §5 codex 指摘 D-M4): 損失・リスク・notional の
        口座通貨換算は口座通貨額を**過小評価しない側**を使う — 直接ペアは
        ask (価格が高いほど換算後の額が大きい)、逆ペアは 1/bid (bid が
        小さいほど逆数が大きい)。mid は表示・分析用でここでは使わない。
        """
        symbol, invert = spec
        q = self.get_quote(symbol)      # 鮮度・有限性・正値の検証を通る
        price = q.bid if invert else q.ask
        if not math.isfinite(price) or price <= 0:
            raise DataUnhealthy(f"invalid price for {symbol}: {price}")
        rate = 1.0 / price if invert else price
        return rate, q.ts

    def to_account_rate(self, ccy: str, account_ccy: str, *,
                        reference_ts: datetime,
                        max_skew_min: float) -> ConversionRate:
        """通貨 1 単位 = 口座通貨いくらか (設計書 §5「口座通貨と換算」)。

        quote/base のどちらの通貨にも使う汎用関数 (旧
        `quote_to_account_rate` — 互換 alias は作らない。呼び出し側を
        全て更新する)。①直接ペア → ②逆ペア (逆数) → ③USD 経由のクロス の
        順に解決し、**保守側** (`_rate_of` 参照) を使う。

        `reference_ts`/`max_skew_min`: この換算が使われる判断 (gate 評価・
        予約再検証サイクル) の基準時刻と許容skew。各脚の鮮度は quote 取得時
        (`get_quote` → `validate_quote`) に検証済みだが、①クロス脚同士の
        時刻差 ②この判断全体とのスナップショット時刻差 は
        `validate_conversion_skew` で追加検証する (codex 指摘 D-I2)。

        いずれも解決不可、または skew 超過なら DataUnhealthy。呼び出し側
        (sizing / risk_gate / executor / scheduler) はこれを fail closed
        (sizing は SizingError、gate/executor は却下、予約再検証は当該ペアの
        pending_fill 取消) に変換する — 推測値でのサイジングは無音の過大
        建玉になるため、ここで握りつぶさない。
        """
        if ccy == account_ccy:
            return ConversionRate(1.0, ccy, account_ccy, (reference_ts,))
        direct = self._rate_symbol(ccy, account_ccy)
        if direct is not None:
            rate, ts = self._rate_of(direct)
            result = ConversionRate(rate, ccy, account_ccy, (ts,))
        else:
            # ③ USD 経由のクロス (例: EUR→JPY = EURUSD(ask) × USDJPY(ask))
            legs: list[tuple[str, bool]] = []
            for base, quote in ((ccy, "USD"), ("USD", account_ccy)):
                if base == quote:
                    continue            # 片脚が USD 同士なら換算不要
                spec = self._rate_symbol(base, quote)
                if spec is None:
                    raise DataUnhealthy(
                        f"cannot convert {ccy}->{account_ccy}: no symbol for "
                        f"{base}/{quote} in VENDOR_SYMBOLS")
                legs.append(spec)
            if not legs:
                # ccy == account_ccy は先に返しているため到達しない
                raise DataUnhealthy(
                    f"cannot convert {ccy}->{account_ccy}: no route")
            rate = 1.0
            leg_ts: list[datetime] = []
            for spec in legs:
                r, ts = self._rate_of(spec)
                rate *= r
                leg_ts.append(ts)
            result = ConversionRate(rate, ccy, account_ccy, tuple(leg_ts))
        validate_conversion_skew(result, reference_ts=reference_ts,
                                 max_skew_min=max_skew_min)
        return result
