"""MetaTrader5 Python パッケージの薄いラッパー。

read-only 操作のみ実装。発注・ポジション操作は意図的に未実装。

`MetaTrader5` は Windows でのみ pip install できるため、Linux 上でも
import エラーにならないよう lazy import + skeleton stub を提供する
(open-source 化や CI 上での skeleton チェック用)。
"""
from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

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
_MEASURE_INTERVAL_SEC = 60
_MEASURE_MAX_AGE_SEC = 300
NY = ZoneInfo("America/New_York")
SERVER_AHEAD_OF_NY = timedelta(hours=7)


class TimeRuleUnavailableError(RuntimeError):
    """Market data is unavailable until the broker clock matches the rule."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _new_york_wall_to_utc(wall: datetime) -> datetime:
    first = wall.replace(tzinfo=NY, fold=0)
    second = wall.replace(tzinfo=NY, fold=1)
    first_utc = first.astimezone(timezone.utc)
    second_utc = second.astimezone(timezone.utc)
    first_round_trip = first_utc.astimezone(NY).replace(tzinfo=None) == wall
    second_round_trip = second_utc.astimezone(NY).replace(tzinfo=None) == wall
    if first_round_trip and second_round_trip and first_utc != second_utc:
        logger.warning("ambiguous New York server wall time: %s", wall.isoformat())
        return first_utc
    if not first_round_trip or not second_round_trip:
        logger.warning("nonexistent New York server wall time: %s", wall.isoformat())
        return first_utc
    return first_utc


def server_epoch_to_utc(server_epoch: int) -> datetime:
    """Convert an MT5 server-wall-clock epoch to an aware UTC datetime."""
    server_wall = datetime.fromtimestamp(server_epoch, timezone.utc).replace(tzinfo=None)
    return _new_york_wall_to_utc(server_wall - SERVER_AHEAD_OF_NY)


def utc_to_server_epoch(utc: datetime) -> int:
    """Encode an aware UTC instant as an MT5 server-wall-clock epoch."""
    if utc.tzinfo is None or utc.utcoffset() != timedelta(0):
        raise TypeError("utc must be an aware datetime with a zero UTC offset")
    server_wall = utc.astimezone(NY).replace(tzinfo=None) + SERVER_AHEAD_OF_NY
    return int(server_wall.replace(tzinfo=timezone.utc).timestamp())


def _market_is_open(now: datetime) -> bool:
    local = now.astimezone(NY)
    trading_day = (local.replace(tzinfo=None) + SERVER_AHEAD_OF_NY).date()
    if (trading_day.month, trading_day.day) in {(12, 25), (1, 1)}:
        return False
    if local.weekday() == 4:
        return local.time() < wall_time(17)
    if local.weekday() == 5:
        return False
    if local.weekday() == 6:
        return local.time() >= wall_time(17)
    return True


def _expected_offset(now: datetime) -> int:
    return utc_to_server_epoch(now) - int(now.timestamp())


def _validate_time_rule_data() -> None:
    try:
        summer = datetime(2026, 7, 1, tzinfo=NY).utcoffset()
        winter = datetime(2026, 1, 15, tzinfo=NY).utcoffset()
    except Exception as exc:  # pragma: no cover - platform zoneinfo failure
        raise RuntimeError("America/New_York time-zone data is unavailable") from exc
    if summer != timedelta(hours=-4) or winter != timedelta(hours=-5):
        raise RuntimeError("America/New_York time-zone data failed self-check")


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
    _probe_symbol: str = "USDJPY"        # オフセット検出だけに使う symbol
    _last_measure_attempt_at: float = 0.0
    _measured_offset_sec: int | None = None
    _measured_at: float | None = None
    _match_streak: int = 0
    _mismatch_latched: bool = False
    _last_time_rule_state: str = "unverified"   # 起動直後の状態。最初の遷移から WARNING を出す

    def __init__(
        self, login: int, password: str, server: str,
        *,
        probe_symbol: str = "USDJPY",
    ) -> None:
        _validate_time_rule_data()
        self._login = login
        self._password = password
        self._server = server
        self._mt5: Any = None  # MetaTrader5 module
        self._connected = False
        self._lock = threading.Lock()  # MT5 単一接続への並行アクセスを直列化
        self._probe_symbol = probe_symbol
        self._last_measure_attempt_at = 0.0
        self._measured_offset_sec = None
        self._measured_at = None
        self._match_streak = 0
        self._mismatch_latched = False
        self._last_time_rule_state = "unverified"

    def connect(self) -> None:
        """MT5 ターミナルを起動 (or 既起動なら attach) し、必要なときだけログインする。

        OANDA は 2026-10-31 から MT5 ログインに TOTP を必須化した。Python API の
        login() は新規認証として扱われ OTP を要求して失敗する
        (-7, 'Unsupported authorization mode, OTP or certificate password needed')。
        一方 initialize() だけならターミナル側で既に認証済みの口座に attach できるため、
        ターミナルが同じ口座でログイン済みなら login() を呼ばない。これが無人運用の
        唯一の経路。別口座 / 未ログインのときは従来どおり login() を試みる。
        """
        self._mt5 = _import_mt5()
        with self._lock:
            if not self._mt5.initialize():
                err = self._mt5.last_error()
                raise RuntimeError(f"MT5 initialize() failed: {err}")
            info = self._mt5.account_info()
            # 防御的に両辺 int 比較 (login が str で返っても attach 判定を落とさない)
            current_login = int(getattr(info, "login", 0) or 0) if info is not None else 0
            if info is not None and current_login == int(self._login):
                logger.info(
                    f"MT5 attached to already-authorized account {self._login}; "
                    "skipping login()"
                )
            elif not self._mt5.login(login=self._login, password=self._password,
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

    # ── サーバ時刻規則の検査 ──────────────────────────────────────

    @property
    def offset_source(self) -> str:
        return "rule"

    def get_server_offset_sec(self) -> int:
        return _expected_offset(self._now())

    def get_server_time(self) -> dict:
        """Return rule-derived server time and the latest measurement evidence."""
        with self._lock:
            now = self._now()
            self._maybe_measure_locked(now)
            state = self._time_rule_state_locked(now)
            measured_at = self._measured_at
            age = None if measured_at is None else int(now.timestamp() - measured_at)
            measured_offset = self._measured_offset_sec
            expected = _expected_offset(now)
            streak = self._match_streak
            measured_expected = (None if measured_at is None else
                                 _expected_offset(datetime.fromtimestamp(
                                     measured_at, timezone.utc)))
        return {
            "server_offset_sec": expected,
            "server_time": datetime.fromtimestamp(
                utc_to_server_epoch(now), timezone.utc).isoformat(),
            "utc_time": now.isoformat(),
            "offset_source": "rule",
            "measured_offset_sec": measured_offset,
            "measured_at": (None if measured_at is None else
                            datetime.fromtimestamp(measured_at, timezone.utc).isoformat()),
            "measured_age_sec": age,
            "expected_offset_sec": measured_expected,
            "offset_matches_rule": (None if measured_offset is None else
                                    measured_offset == measured_expected),
            "match_streak": (None if measured_at is None else streak),
            "time_rule_state": state,
        }

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=timezone.utc)

    def _maybe_measure_locked(self, now: datetime, *, symbol: str | None = None,
                              tick_time: int | None = None) -> None:
        if not _market_is_open(now):
            return
        now_epoch = now.timestamp()
        if now_epoch - self._last_measure_attempt_at < _MEASURE_INTERVAL_SEC:
            return
        self._last_measure_attempt_at = now_epoch
        ticks = [tick_time] if tick_time is not None else []
        for candidate in {self._probe_symbol, symbol} - {None}:
            value = self._probe_tick_time_locked(candidate)
            if value is not None:
                ticks.append(value)
        if not ticks:
            return
        accepted = [(value, snap_server_offset(value - now_epoch)) for value in ticks]
        accepted = [(value, offset) for value, offset in accepted if offset is not None]
        if not accepted:
            return
        _newest_tick, snapped = max(accepted)
        self._measured_offset_sec = snapped
        self._measured_at = now_epoch
        if snapped == _expected_offset(now):
            self._match_streak += 1
            if self._mismatch_latched and self._match_streak >= 3:
                self._mismatch_latched = False
        else:
            self._mismatch_latched = True
            self._match_streak = 0

    def _time_rule_state_locked(self, now: datetime) -> str:
        if not _market_is_open(now):
            state = "unverified"
        elif self._mismatch_latched:
            state = "mismatch"
        elif self._measured_at is None or now.timestamp() - self._measured_at > _MEASURE_MAX_AGE_SEC:
            state = "unverified"
        else:
            state = "ok"
        if state != self._last_time_rule_state:
            logger.warning("MT5 time rule state: %s", state)
            self._last_time_rule_state = state
        return state

    def _require_market_data_time_rule_locked(self, now: datetime) -> None:
        state = self._time_rule_state_locked(now)
        if state != "ok":
            raise TimeRuleUnavailableError(f"time_rule_{state}")

    def time_rule_status(self) -> tuple[str, str | None]:
        with self._lock:
            now = self._now()
            state = self._time_rule_state_locked(now)
            measured_at = (None if self._measured_at is None else
                           datetime.fromtimestamp(self._measured_at, timezone.utc).isoformat())
            return state, measured_at

    def _probe_tick_time_locked(self, symbol: str) -> int | None:
        """Return a tick timestamp for rule verification, or ``None`` on failure."""
        try:
            if not self._mt5.symbol_select(symbol, True):
                return None
            tick = self._mt5.symbol_info_tick(symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"time-rule probe failed on {symbol}: {e}")
            return None
        if tick is None:
            return None
        return int(tick.time)

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
            now = self._now()
            self._maybe_measure_locked(now, symbol=symbol, tick_time=int(tick.time))
            self._require_market_data_time_rule_locked(now)
            ts = server_epoch_to_utc(int(tick.time)).isoformat()
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
            now = self._now()
            self._maybe_measure_locked(now)
            self._time_rule_state_locked(now)
        if positions is None:
            return []
        result: list[Position] = []
        for p in positions:
            # MT5 type: 0=buy, 1=sell
            ptype = "buy" if p.type == 0 else "sell"
            # p.time はサーバ時刻の epoch
            ts = server_epoch_to_utc(int(p.time)).isoformat()
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
            now = self._now()
            self._maybe_measure_locked(now)
            self._time_rule_state_locked(now)
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
            closed_at=server_epoch_to_utc(closed_ts).isoformat(),
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
        規則の逆 (UTC -> NY 壁時計 + 7h) で変換し、返ってきた足の時刻は足ごとに
        規則で UTC に戻す。片方向だけ直すと範囲が静かに切り詰められる。
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

            self._maybe_measure_locked(self._now(), symbol=symbol)

            rates = self._mt5.copy_rates_range(
                symbol, tf,
                datetime.fromtimestamp(
                    utc_to_server_epoch(date_from.astimezone(timezone.utc)), timezone.utc),
                datetime.fromtimestamp(
                    utc_to_server_epoch(date_to.astimezone(timezone.utc)), timezone.utc),
            )
            if rates is None:
                raise RuntimeError(
                    f"copy_rates_range failed: {self._mt5.last_error()}"
                )

            bars: list[dict] = []
            for r in rates:
                # rates fields: time, open, high, low, close, tick_volume, spread, real_volume
                # r["time"] はサーバ時刻の epoch
                ts = server_epoch_to_utc(int(r["time"])).isoformat()
                bars.append({
                    "time": ts,
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r["tick_volume"]),
                })
            # 取得の間に時計が進んでいても、応答時点で検査が新しいことを保つ。
            now = self._now()
            self._maybe_measure_locked(now, symbol=symbol)
            self._require_market_data_time_rule_locked(now)
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
