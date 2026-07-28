"""MetaTrader5 Python パッケージの薄いラッパー。

read-only 操作のみ実装。発注・ポジション操作は意図的に未実装。

`MetaTrader5` は Windows でのみ pip install できるため、Linux 上でも
import エラーにならないよう lazy import + skeleton stub を提供する
(open-source 化や CI 上での skeleton チェック用)。
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── サーバ時刻オフセット ───────────────────────────────────────────
# MT5 が返す時刻 (tick.time / position.time / deal.time / rates["time"]) は
# すべて「ブローカーのサーバ時間帯におけるエポック秒」であり UTC ではない。
# copy_rates_range の date_from / date_to も同じくサーバ時刻空間で解釈される。
# 実測 (2026-07-28、OANDA-Japan MT5 Live): サーバ時刻 = UTC+3。
#
# API にサーバのタイムゾーンを直接返すものは無いため、
# `symbol_info_tick(symbol).time` とこちらの UTC 時計の差から推定する。

_OFFSET_MAX_ABS_SEC = 12 * 3600      # ガード 1: ±12h 超は tick が古い証拠
_OFFSET_SNAP_SEC = 1800              # ガード 2: 30 分単位に丸める
_OFFSET_MAX_RESIDUAL_SEC = 120       # ガード 3: 丸め残差の許容
_OFFSET_RECALC_INTERVAL_SEC = 300    # 再計算の最短間隔 (毎リクエスト叩かない)


class ServerTimeUnknownError(RuntimeError):
    """サーバ時刻オフセットが一度も確定していない (fail closed)。

    推測値で時刻を返すと、ずれた足でサイジングしたり誤った時刻で発注したり
    する。止まる方が安全なので例外を上げ、server 側は 503 に map する。
    RuntimeError を継承しているのは、まだ個別 except を持たない呼出元でも
    「無言で誤った値が返る」ことだけは起きないようにするため。
    """


def snap_server_offset(raw_sec: float) -> int | None:
    """tick 時刻とこちらの時計の差 (raw) から採用可能なオフセットを求める。

    raw = tick.time - time.time()。市場が閉まっている間 tick は古いので
    raw を単独では信用できない (週末は最大 ~48h ずれる)。以下のガードを通った
    ものだけ採用し、通らなければ None (= 呼出側で直前の good 値へフォールバック)。

    1. |raw| > 12h → 棄却。ブローカーのサーバ時刻オフセットが ±12h を超えることは
       実務上なく、超えるのは tick が古い証拠 (週末の 48h ずれはここで落ちる)
    2. 30 分単位に丸める。MT5 サーバのオフセットは実務上 30 分の倍数であり、
       丸めることで tick とこちらの時計の秒単位のずれを吸収する
       (実測 raw = 10799.3s → 10800 = 3h ちょうど)
    3. |raw - snapped| > 120s → 棄却。残差が大きいのも tick が古い証拠
    """
    if abs(raw_sec) > _OFFSET_MAX_ABS_SEC:
        return None
    snapped = int(round(raw_sec / _OFFSET_SNAP_SEC) * _OFFSET_SNAP_SEC)
    if abs(raw_sec - snapped) > _OFFSET_MAX_RESIDUAL_SEC:
        return None
    return snapped


def to_server_datetime(utc_dt: datetime, offset_sec: int) -> datetime:
    """送信 (こちら→MT5): 真の UTC をサーバ時刻空間へずらす。"""
    return utc_dt + timedelta(seconds=offset_sec)


def from_server_epoch(server_epoch: int, offset_sec: int) -> datetime:
    """受信 (MT5→こちら): サーバ時刻のエポック秒を真の UTC の datetime に直す。"""
    return datetime.fromtimestamp(
        int(server_epoch) - offset_sec, tz=timezone.utc,
    )


def _import_mt5():
    """Windows のみ MetaTrader5 を import。それ以外は ImportError を投げる。"""
    if sys.platform != "win32":
        raise ImportError(
            "MetaTrader5 package is Windows-only. "
            "Run this bridge on Windows (or via Wine, unsupported)."
        )
    import MetaTrader5 as mt5  # noqa: PLC0415 — lazy import 必須
    return mt5


@dataclass
class AccountInfo:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    leverage: int
    name: str
    trade_mode: int  # 0=demo, 1=contest, 2=real (MT5 仕様)


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    spread_points: int
    time: str  # ISO 8601


@dataclass
class Position:
    ticket: int
    symbol: str
    type: str          # "buy" | "sell"
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float
    swap: float
    magic: int
    comment: str
    time: str          # ISO 8601


@dataclass
class ClosedDeal:
    ticket: int
    close_price: float
    profit: float
    swap: float
    commission: float
    closed_at: str
    reason: str


class PreflightError(Exception):
    """order pre-flight 検証の失敗 (symbol 不可 / tick 無し / 証拠金不足)。

    server 側で HTTP status に map する (http_status を持つ)。
    """
    def __init__(self, message: str, *, http_status: int = 422) -> None:
        super().__init__(message)
        self.http_status = http_status


class Mt5Client:
    """MT5 ターミナルへの接続を保持し、read-only 照会だけを提供する。"""

    # サーバ時刻オフセットの状態。クラス属性として既定値を持たせているのは、
    # 既存テストが `Mt5Client.__new__(Mt5Client)` で __init__ を経由せずに
    # インスタンスを組み立てているため (AttributeError にしない)。
    _server_offset_sec: int | None = None
    _offset_checked_at: float = 0.0      # 最後に「検出を試みた」時刻 (time.time())
    _offset_source: str = "unknown"      # "live" | "cached" | "unknown"
    _offset_cache_path: Path | None = None
    _offset_disk_loaded: bool = False
    _probe_symbol: str = "USDJPY"        # オフセット検出だけに使う symbol

    def __init__(
        self, login: int, password: str, server: str,
        *,
        offset_cache_path: Path | str | None = None,
        probe_symbol: str = "USDJPY",
    ) -> None:
        self._login = login
        self._password = password
        self._server = server
        self._mt5: Any = None  # MetaTrader5 module
        self._connected = False
        self._lock = threading.Lock()  # MT5 単一接続への並行アクセスを直列化
        # オフセットは週末をまたぐ再起動でも残るようディスクにも置く
        self._offset_cache_path = (
            Path(offset_cache_path) if offset_cache_path is not None else None
        )
        self._probe_symbol = probe_symbol
        self._server_offset_sec = None
        self._offset_checked_at = 0.0
        self._offset_source = "unknown"
        self._offset_disk_loaded = False

    def connect(self) -> None:
        """MT5 ターミナルを起動 (or 既起動なら attach) し、ログインする。"""
        self._mt5 = _import_mt5()
        with self._lock:
            if not self._mt5.initialize():
                err = self._mt5.last_error()
                raise RuntimeError(f"MT5 initialize() failed: {err}")
            if not self._mt5.login(login=self._login, password=self._password,
                                   server=self._server):
                err = self._mt5.last_error()
                self._mt5.shutdown()
                raise RuntimeError(f"MT5 login failed: {err}")
            self._connected = True
        logger.info(f"MT5 connected: login={self._login} server={self._server}")

    def disconnect(self) -> None:
        with self._lock:
            if self._mt5 is not None and self._connected:
                self._mt5.shutdown()
                self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def ping(self) -> bool:
        """terminal_info() で生存確認。"""
        if not self._connected:
            return False
        try:
            with self._lock:
                info = self._mt5.terminal_info()
            return info is not None
        except Exception:  # noqa: BLE001
            return False

    # ── サーバ時刻オフセット ──────────────────────────────────────
    # 検出は必ず lock の内側で行う (`self._mt5` を lock 外で触らない)。
    # 公開 API は get_server_offset_sec() / get_server_time() の 2 つ。

    @property
    def offset_source(self) -> str:
        """直近の値の出所。"live" = 直近の検出が成功、"cached" = 前回の good 値。"""
        return self._offset_source

    def get_server_offset_sec(self) -> int:
        """サーバ時刻オフセット (秒)。確定できなければ ServerTimeUnknownError。"""
        with self._lock:
            return self._ensure_offset_locked()

    def get_server_time(self) -> dict:
        """こちらの UTC とブローカーのサーバ時刻を並べて返す (観測・切り分け用)。"""
        with self._lock:
            offset = self._ensure_offset_locked()
            # source も同じ lock 区間で読む。lock を離してから読むと、その隙に
            # 別リクエストが再検出して source だけ書き換わり、返した offset とは
            # 別の解決結果が混ざる (/server-time は検証の窓口なので対を崩さない)
            source = self._offset_source
        now = datetime.now(tz=timezone.utc)
        return {
            "server_offset_sec": offset,
            "server_time": to_server_datetime(now, offset).isoformat(),
            "utc_time": now.isoformat(),
            "offset_source": source,
        }

    def _load_offset_from_disk(self) -> None:
        """起動後 1 回だけディスクの good 値を読む (週末をまたぐ再起動用)。

        読めた値も ±12h ガードにかける。壊れていれば無視するだけで、
        推測値は決して採用しない (good 値なしのまま fail closed に倒す)。
        """
        self._offset_disk_loaded = True
        path = self._offset_cache_path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = int(data["server_offset_sec"])
        except Exception as e:  # noqa: BLE001 — 壊れたキャッシュは黙って捨てる
            logger.warning(f"server offset cache unreadable ({path}): {e}")
            return
        if abs(value) > _OFFSET_MAX_ABS_SEC:
            logger.warning(
                f"server offset cache out of range: {value}s (ignored)"
            )
            return
        self._server_offset_sec = value
        self._offset_source = "cached"
        # _offset_checked_at は 0 のまま = 起動直後に必ず実測を試みる。
        # (夏時間切替をまたぐ再起動でディスクの古い値に居座らないため)
        logger.info(f"server offset loaded from cache: {value}s ({path})")

    def _save_offset_to_disk(self, offset_sec: int) -> None:
        path = self._offset_cache_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({
                    "server_offset_sec": offset_sec,
                    "updated_at": datetime.now(tz=timezone.utc).isoformat(),
                    "mt5_server": self._server,
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            # 永続化に失敗してもメモリ上の good 値では動ける (取引は止めない)
            logger.warning(f"failed to persist server offset to {path}: {e}")

    def _probe_tick_time_locked(self, symbol: str) -> int | None:
        """オフセット検出用に tick を 1 本取り、そのサーバ時刻 (epoch) を返す。

        失敗しても例外にしない。検出できなかった扱い (= 直前の good 値へ
        フォールバック、good 値が無ければ呼出側で fail closed) にする。
        """
        try:
            if not self._mt5.symbol_select(symbol, True):
                return None
            tick = self._mt5.symbol_info_tick(symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"server offset probe failed on {symbol}: {e}")
            return None
        if tick is None:
            return None
        return int(tick.time)

    def _ensure_offset_locked(
        self, *, tick_time: int | None = None, probe_symbol: str | None = None,
    ) -> int:
        """オフセットを返す。必要なら再検出する。lock 保持下で呼ぶこと。

        tick_time: 呼出側が既に取得済みの tick のサーバ時刻 (get_quote 用)。
                   渡されていれば MT5 を余分に叩かない。
        probe_symbol: tick_time が無いときに probe する symbol。
        """
        if not self._offset_disk_loaded:
            self._load_offset_from_disk()

        now = time.time()
        # 再計算は最短 _OFFSET_RECALC_INTERVAL_SEC 間隔。窓が開いていなければ
        # MT5 を一切叩かずキャッシュを返す (毎リクエスト probe しない)。
        if (self._server_offset_sec is not None
                and now - self._offset_checked_at < _OFFSET_RECALC_INTERVAL_SEC):
            return self._server_offset_sec

        if tick_time is None:
            tick_time = self._probe_tick_time_locked(
                probe_symbol or self._probe_symbol,
            )
        self._offset_checked_at = now

        snapped = (
            snap_server_offset(float(tick_time) - now)
            if tick_time is not None else None
        )
        if snapped is not None:
            if snapped != self._server_offset_sec:
                logger.info(
                    f"MT5 server time offset detected: {snapped}s "
                    f"(was {self._server_offset_sec})"
                )
                self._server_offset_sec = snapped
                self._save_offset_to_disk(snapped)
            self._offset_source = "live"
            return snapped

        # 棄却 (tick が古い / 取れない)。直前の good 値でしのぐ (週末はこの経路)。
        self._offset_source = "cached"
        if self._server_offset_sec is None:
            raise ServerTimeUnknownError(
                "MT5 server time offset is unknown: no fresh tick and no cached "
                "value. Refusing to report times rather than guessing "
                "(fail closed)."
            )
        return self._server_offset_sec

    def get_account(self) -> AccountInfo:
        with self._lock:
            return self._get_account_locked()

    def _get_account_locked(self) -> AccountInfo:
        info = self._mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info() failed: {self._mt5.last_error()}")
        return AccountInfo(
            login=info.login,
            server=info.server,
            currency=info.currency,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            free_margin=info.margin_free,
            leverage=info.leverage,
            name=info.name,
            trade_mode=info.trade_mode,
        )

    def get_quote(self, symbol: str) -> Quote:
        with self._lock:
            # symbol_info_tick がスプレッド込みの最新 bid/ask を返す
            if not self._mt5.symbol_select(symbol, True):
                raise RuntimeError(f"symbol_select({symbol}) failed: {self._mt5.last_error()}")
            tick = self._mt5.symbol_info_tick(symbol)
            if tick is None:
                raise RuntimeError(f"symbol_info_tick({symbol}) failed: {self._mt5.last_error()}")
            info = self._mt5.symbol_info(symbol)
            spread = int(info.spread) if info is not None else 0
            # tick.time はサーバ時刻。ここで取れた tick はオフセット検出の
            # 材料としても使える (MT5 を余分に叩かずに済む)。
            offset = self._ensure_offset_locked(
                tick_time=int(tick.time), probe_symbol=symbol,
            )
            ts = from_server_epoch(tick.time, offset).isoformat()
            return Quote(
                symbol=symbol, bid=float(tick.bid), ask=float(tick.ask),
                spread_points=spread, time=ts,
            )

    def place_order_dry_run(
        self, symbol: str, side: str, volume_lots: float,
        sl: float | None = None, tp: float | None = None,
        magic: int = 0, comment: str = "",
    ) -> dict:
        """発注をシミュレート (MT5 には送らない)。
        現在の bid/ask を fill_price として使い、time_ns で擬似 ticket を生成する。
        """
        import time as _time
        from datetime import datetime, timezone

        with self._lock:
            if not self._mt5.symbol_select(symbol, True):
                raise RuntimeError(
                    f"symbol_select({symbol}) failed: {self._mt5.last_error()}"
                )
            tick = self._mt5.symbol_info_tick(symbol)
            if tick is None:
                raise RuntimeError(
                    f"symbol_info_tick({symbol}) failed: {self._mt5.last_error()}"
                )
            fill_price = float(tick.ask if side == "buy" else tick.bid)
        ticket = _time.time_ns() // 1_000  # μs 解像度の擬似 ticket
        ts = datetime.now(tz=timezone.utc).isoformat()
        logger.info(
            f"[DRY_RUN] order: {side} {symbol} lot={volume_lots} "
            f"@ {fill_price} sl={sl} tp={tp} magic={magic} ticket={ticket}"
        )
        return {
            "ticket": ticket, "symbol": symbol, "side": side,
            "volume_lots": volume_lots, "fill_price": fill_price,
            "sl": sl, "tp": tp, "time": ts,
            "dry_run": True, "magic": magic,
        }

    def close_position_dry_run(
        self, ticket: int, symbol: str | None = None,
    ) -> dict:
        """ポジション close をシミュレート。

        DRY_RUN ではポジション ticket は擬似値なので MT5 の positions_get には存在しない。
        symbol が指定されていれば対向 tick の中値を close_price として返す。
        symbol 未指定なら 0.0 を返す (シャドウ用途では adapter 側で symbol を保持)。
        """
        from datetime import datetime, timezone

        close_price = 0.0
        if symbol:
            with self._lock:
                tick = self._mt5.symbol_info_tick(symbol)
            if tick is not None:
                close_price = float((tick.bid + tick.ask) / 2)
        ts = datetime.now(tz=timezone.utc).isoformat()
        logger.info(
            f"[DRY_RUN] close: ticket={ticket} symbol={symbol} @ {close_price}"
        )
        return {
            "ticket": ticket, "close_price": close_price, "time": ts,
            "dry_run": True, "note": "DRY_RUN: ticket not validated",
        }

    def get_positions(self) -> list[Position]:
        with self._lock:
            positions = self._mt5.positions_get()
            # offset の解決は lock の内側で行う (probe が MT5 を叩くため)。
            # 整形自体は lock の外でよいので、値だけ持ち出す。
            offset = self._ensure_offset_locked()
        if positions is None:
            return []
        result: list[Position] = []
        for p in positions:
            # MT5 type: 0=buy, 1=sell
            ptype = "buy" if p.type == 0 else "sell"
            # p.time はサーバ時刻の epoch
            ts = from_server_epoch(p.time, offset).isoformat()
            result.append(Position(
                ticket=p.ticket, symbol=p.symbol, type=ptype, volume=p.volume,
                price_open=p.price_open, price_current=p.price_current,
                sl=p.sl, tp=p.tp, profit=p.profit, swap=p.swap,
                magic=p.magic, comment=p.comment, time=ts,
            ))
        return result

    def get_closed_deal(self, ticket: int) -> ClosedDeal | None:
        """指定 position/order ticket に紐づく close deal 実績を返す。

        server-side SL/TP で position が消えた後の reconciliation で、検知時点の
        current price ではなく MT5 の実決済価格・実現損益を使うための参照。
        """
        with self._lock:
            try:
                deals = self._mt5.history_deals_get(position=ticket)
            except TypeError:
                deals = None
            # offset の解決は lock の内側で (probe が MT5 を叩くため)
            offset = self._ensure_offset_locked()
        if not deals:
            return None

        entry_out = {
            getattr(self._mt5, "DEAL_ENTRY_OUT", 1),
            getattr(self._mt5, "DEAL_ENTRY_OUT_BY", 3),
        }
        close_deals = [
            d for d in deals
            if getattr(d, "entry", None) in entry_out
        ]
        if not close_deals:
            return None

        total_volume = sum(abs(float(getattr(d, "volume", 0.0))) for d in close_deals)
        if total_volume > 0:
            close_price = sum(
                float(getattr(d, "price", 0.0))
                * abs(float(getattr(d, "volume", 0.0)))
                for d in close_deals
            ) / total_volume
        else:
            close_price = float(getattr(close_deals[-1], "price", 0.0))

        profit = sum(float(getattr(d, "profit", 0.0)) for d in close_deals)
        swap = sum(float(getattr(d, "swap", 0.0)) for d in close_deals)
        commission = sum(float(getattr(d, "commission", 0.0)) for d in close_deals)
        closed_ts = max(int(getattr(d, "time", 0)) for d in close_deals)
        reason = str(getattr(close_deals[-1], "reason", ""))

        return ClosedDeal(
            ticket=ticket,
            close_price=close_price,
            profit=profit,
            swap=swap,
            commission=commission,
            # d.time はサーバ時刻の epoch
            closed_at=from_server_epoch(closed_ts, offset).isoformat(),
            reason=reason,
        )

    def get_symbols(self) -> list[str]:
        with self._lock:
            symbols = self._mt5.symbols_get()
        if symbols is None:
            return []
        return [s.name for s in symbols]

    def copy_rates_range(
        self, symbol: str, interval: str,
        date_from: Any, date_to: Any,
    ) -> list[dict]:
        """MT5 から OHLCV を取得し dict のリストで返す。

        interval: "1m" | "5m" | "15m" | "30m" | "1h" | "4h" | "1d"
        date_from / date_to: tz-aware datetime (naive は拒否)

        MT5 は date_from / date_to も**サーバ時刻空間**で解釈するため、送信前に
        +offset し、返ってきたバー時刻からは -offset する。片方向だけ直すと
        範囲が静かに切り詰められる (実測: to を実 UTC のままにすると末尾 3 時間分の
        足が返らない)。
        """
        # naive datetime は「どの時間帯の壁時計か」が決まらない。ここで +offset
        # しても意味が定まらないので受け取らない (プロジェクト制約: tz-aware UTC)。
        for name, d in (("date_from", date_from), ("date_to", date_to)):
            if getattr(d, "tzinfo", None) is None or d.utcoffset() is None:
                raise ValueError(
                    f"{name} must be a tz-aware datetime (naive datetime rejected)"
                )

        # interval -> MT5 TIMEFRAME 定数の属性名 (lock 外で軽く解決)。
        tf_attr_map = {
            "1m":  "TIMEFRAME_M1",
            "5m":  "TIMEFRAME_M5",
            "15m": "TIMEFRAME_M15",
            "30m": "TIMEFRAME_M30",
            "1h":  "TIMEFRAME_H1",
            "4h":  "TIMEFRAME_H4",
            "1d":  "TIMEFRAME_D1",
        }
        tf_attr = tf_attr_map.get(interval)
        if tf_attr is None:
            raise ValueError(f"unsupported interval: {interval}")

        with self._lock:
            tf = getattr(self._mt5, tf_attr)
            if not self._mt5.symbol_select(symbol, True):
                raise RuntimeError(
                    f"symbol_select({symbol}) failed: {self._mt5.last_error()}"
                )

            # symbol_select 済みなので、この symbol をそのまま probe に使える
            offset = self._ensure_offset_locked(probe_symbol=symbol)

            rates = self._mt5.copy_rates_range(
                symbol, tf,
                to_server_datetime(date_from, offset),
                to_server_datetime(date_to, offset),
            )
            if rates is None:
                raise RuntimeError(
                    f"copy_rates_range failed: {self._mt5.last_error()}"
                )

            bars: list[dict] = []
            for r in rates:
                # rates fields: time, open, high, low, close, tick_volume, spread, real_volume
                # r["time"] はサーバ時刻の epoch
                ts = from_server_epoch(int(r["time"]), offset).isoformat()
                bars.append({
                    "time": ts,
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r["tick_volume"]),
                })
            return bars

    def calc_required_margin(
        self, symbol: str, side: str, volume_lots: float, price: float,
    ) -> float:
        """MT5 内蔵関数で必要証拠金を計算 (口座通貨建て、通貨換算込み)。"""
        with self._lock:
            return self._calc_required_margin_locked(symbol, side, volume_lots, price)

    def _calc_required_margin_locked(
        self, symbol: str, side: str, volume_lots: float, price: float,
    ) -> float:
        action = (
            self._mt5.ORDER_TYPE_BUY if side == "buy" else self._mt5.ORDER_TYPE_SELL
        )
        margin = self._mt5.order_calc_margin(action, symbol, volume_lots, price)
        if margin is None:
            raise RuntimeError(
                f"order_calc_margin failed: {self._mt5.last_error()}"
            )
        return float(margin)

    def modify_position_dry_run(
        self, ticket: int, *, sl: float | None = None, tp: float | None = None,
    ) -> dict:
        """SL/TP modify をシミュレートする。MT5 には送らない。"""
        return {
            "ticket": ticket,
            "symbol": "DRYRUN",
            "sl": sl,
            "tp": tp,
            "retcode": None,
            "comment": "DRY_RUN: position modify not sent",
            "dry_run": True,
        }

    def modify_position_live(
        self, ticket: int, *, sl: float | None = None, tp: float | None = None,
    ) -> dict:
        """MT5 の既存ポジション SL/TP を変更する。

        片方だけ指定された場合、未指定側は現在ポジションの値を保持する。
        """
        with self._lock:
            positions = self._mt5.positions_get(ticket=ticket)
            if not positions:
                raise RuntimeError(f"position {ticket} not found")
            p = positions[0]
            next_sl = float(p.sl) if sl is None else float(sl)
            next_tp = float(p.tp) if tp is None else float(tp)
            request = {
                "action": self._mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "symbol": p.symbol,
                "sl": next_sl,
                "tp": next_tp,
            }
            result = self._mt5.order_send(request)
            if result is None:
                raise RuntimeError(f"modify_position {ticket} failed: order_send returned None: {self._mt5.last_error()}")
            retcode = int(result.retcode)
            done = getattr(self._mt5, "TRADE_RETCODE_DONE", 10009)
            if retcode != done:
                raise RuntimeError(
                    f"modify_position {ticket} failed: retcode={retcode} "
                    f"comment={getattr(result, 'comment', '')}"
                )
            return {
                "ticket": ticket,
                "symbol": p.symbol,
                "sl": next_sl,
                "tp": next_tp,
                "retcode": retcode,
                "comment": str(getattr(result, "comment", "")),
                "dry_run": False,
            }

    def close_position_live(self, ticket: int) -> dict:
        """MT5 のポジションをクローズ。symbol/volume/type は positions_get から取得。"""
        from datetime import datetime, timezone

        with self._lock:
            positions = self._mt5.positions_get(ticket=ticket)
            if not positions:
                raise RuntimeError(f"position {ticket} not found")
            p = positions[0]
            close_type = (
                self._mt5.ORDER_TYPE_SELL if p.type == 0 else self._mt5.ORDER_TYPE_BUY
            )
            tick = self._mt5.symbol_info_tick(p.symbol)
            if tick is None:
                raise RuntimeError(f"no tick for {p.symbol}")
            price = float(tick.bid if p.type == 0 else tick.ask)

            request = {
                "action": self._mt5.TRADE_ACTION_DEAL,
                "position": ticket,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": close_type,
                "price": price,
                "deviation": 30,
                "magic": p.magic,
                "comment": "close by bridge",
                "type_time": self._mt5.ORDER_TIME_GTC,
                "type_filling": self._mt5.ORDER_FILLING_IOC,
            }
            result = self._mt5.order_send(request)
            if result is None or result.retcode != self._mt5.TRADE_RETCODE_DONE:
                raise RuntimeError(
                    f"close_position {ticket} failed: retcode="
                    f"{getattr(result, 'retcode', 'None')}"
                )
            ts = datetime.now(tz=timezone.utc).isoformat()
            return {
                "ticket": ticket,
                "close_price": float(result.price),
                "time": ts,
                "dry_run": False,
                "note": "",
            }

    def preflight_order_margin(
        self, symbol: str, side: str, volume_lots: float,
        *, margin_buffer: float = 1.05,
    ) -> None:
        """live 発注前の証拠金チェック (symbol_select→tick→margin→free_margin) を
        lock 下で実行する。問題があれば PreflightError。price は margin 計算専用
        (place_order_live が自前で tick を取り直すため戻さない)。"""
        with self._lock:
            if not self._mt5.symbol_select(symbol, True):
                raise PreflightError(f"symbol {symbol} not available", http_status=404)
            tick = self._mt5.symbol_info_tick(symbol)
            if tick is None:
                raise PreflightError(f"no tick for {symbol}", http_status=404)
            price = float(tick.ask if side == "buy" else tick.bid)
            required = self._calc_required_margin_locked(symbol, side, volume_lots, price)
            required_with_buffer = required * margin_buffer
            free_margin = self._get_account_locked().free_margin
            if free_margin < required_with_buffer:
                raise PreflightError(
                    f"insufficient margin: required={required:.2f} "
                    f"(with buffer={required_with_buffer:.2f}) "
                    f"free={free_margin:.2f}",
                    http_status=422,
                )

    def place_order_live(
        self, symbol: str, side: str, volume_lots: float,
        sl: float | None = None, tp: float | None = None,
        magic: int = 0, comment: str = "",
        filling_mode: str = "IOC",
        deviation_points: int = 30,
    ) -> dict:
        """MT5 へ実発注。retcode 解釈は呼出側 (server.py) で行う。"""
        from datetime import datetime, timezone

        with self._lock:
            if not self._mt5.symbol_select(symbol, True):
                raise RuntimeError(
                    f"symbol_select({symbol}) failed: {self._mt5.last_error()}"
                )
            tick = self._mt5.symbol_info_tick(symbol)
            if tick is None:
                raise RuntimeError(
                    f"symbol_info_tick({symbol}) failed: {self._mt5.last_error()}"
                )
            price = float(tick.ask if side == "buy" else tick.bid)

            fm_map = {
                "IOC": self._mt5.ORDER_FILLING_IOC,
                "FOK": self._mt5.ORDER_FILLING_FOK,
                "RETURN": self._mt5.ORDER_FILLING_RETURN,
            }
            type_filling = fm_map.get(filling_mode.upper(), self._mt5.ORDER_FILLING_IOC)
            order_type = (
                self._mt5.ORDER_TYPE_BUY if side == "buy" else self._mt5.ORDER_TYPE_SELL
            )

            request = {
                "action": self._mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume_lots,
                "type": order_type,
                "price": price,
                "deviation": deviation_points,
                "magic": magic,
                "comment": comment,
                "type_time": self._mt5.ORDER_TIME_GTC,
                "type_filling": type_filling,
            }
            if sl is not None and sl > 0:
                request["sl"] = sl
            if tp is not None and tp > 0:
                request["tp"] = tp

            result = self._mt5.order_send(request)
            if result is None:
                raise RuntimeError(f"order_send returned None: {self._mt5.last_error()}")

            ts = datetime.now(tz=timezone.utc).isoformat()
            return {
                "retcode": int(result.retcode),
                "ticket": int(result.order),
                "symbol": symbol,
                "side": side,
                "volume_lots": float(result.volume),
                "fill_price": float(result.price) if result.price > 0 else price,
                "sl": sl, "tp": tp,
                "time": ts,
                "dry_run": False,
                "magic": magic,
                "comment_response": str(result.comment),
            }
