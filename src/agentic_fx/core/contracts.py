"""全レイヤー共有の契約型。LLM 出力の構造検証まで担当 (セマンティック検証は risk_gate)。"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal, Protocol


class Action(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    CANCEL = "cancel"
    HOLD = "hold"


class StrategyAction(StrEnum):
    """strategy plugin (evaluate) の出力語彙。既存 Action とは別語彙 —
    "exit" は含めない (プラン 7 D1: exit 表現は levels のみ、evaluate 型の
    毎バー exit 判定は本プランでは未対応)。既存 Action (close/cancel を含む)
    を流用・拡張しない。"""
    OPEN = "open"
    HOLD = "hold"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class EntryType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class Horizon(StrEnum):
    DAY = "day"
    SWING = "swing"


class Origin(StrEnum):
    SCHEDULER = "scheduler"
    ASK = "ask"


class Mode(StrEnum):
    LEARNING = "learning"
    TRADING = "trading"


class OrderStatus(StrEnum):
    APPROVAL_PENDING = "approval_pending"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    PENDING_FILL = "pending_fill"
    PROTECTION_PENDING = "protection_pending"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    CANCEL_UNKNOWN = "cancel_unknown"
    CLOSE_UNKNOWN = "close_unknown"
    SUBMIT_UNKNOWN = "submit_unknown"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    ts: datetime
    source: str


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    interval: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class AccountState:
    balance: float
    equity: float
    currency: str
    ts: datetime
    source: str


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    symbol: str
    pip_size: float
    min_lot: float
    max_lot: float
    lot_step: float
    contract_size: float
    # 設計書 §5「口座通貨と換算」: symbol 文字列の切り出し (`symbol[3:]`) や
    # 部分一致による通貨推測は禁止 (前身 finance で 5 箇所に複製され XAU 等で
    # 誤爆した)。base/quote は必ずここで明示する。
    base_currency: str
    quote_currency: str


@dataclass(frozen=True, slots=True)
class ConversionRate:
    """ある通貨 1 単位 = 口座通貨いくらか (設計書 §5)。裸の float でなく値
    オブジェクトにする理由 (codex 指摘 D-I2): サイジング・リスク評価は
    「この数値がいつ観測されたか」を無視できない — 古いレートでの計算は
    無音の過大建玉になる。

    - `from_ccy`/`to_ccy`: 変換元・変換先の通貨コード。呼び出し側が
      base/quote を取り違えて渡した場合に、数値の偶然の一致に頼らず
      構造的に検出できるようにするためのラベル (base/quote 取り違えの
      変異テストの防御線)。
    - `leg_ts`: 各脚 (直接・逆数ペアは 1 脚、USD 経由のクロスは 2 脚) の
      quote 観測時刻。**同一通貨** (`from_ccy == to_ccy`) は外部取得なしの
      恒等変換であり、`leg_ts` には呼び出し時点の参照時刻を 1 つだけ入れる
      (鮮度・skew 検証を一様に扱うため)。
    """
    value: float
    from_ccy: str
    to_ccy: str
    leg_ts: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class BrokerResult:
    status: Literal["ok", "rejected", "unknown"]
    broker_order_id: str | None = None
    broker_position_id: str | None = None
    message: str = ""


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class FixedClock:
    fixed: datetime

    def now(self) -> datetime:
        return self.fixed


@dataclass(frozen=True, slots=True)
class SystemClock:
    """本番用の実時計 (Clock の唯一の実装。FixedClock はテスト用)。

    **必ず tz-aware な UTC を返す** — `datetime.now()` (naive) や
    `datetime.now().astimezone()` (ローカルの aware) は使わない。naive は
    プロジェクトの絶対制約に反し、ローカル aware は ISO 文字列として比較・
    整列する store 層 (econ_events / snapshots / orders) でオフセットが
    混ざり、範囲検索と順序が無音で壊れる。

    実時計が必要な入口 (init の価格ソース確認、プラン 5 の service 配線) は
    その場で無名クラスを作らずこれを使う — 実装が分散すると片方だけ
    naive に退行しても気づけない。
    """

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class IntentParseError(ValueError):
    pass


_EXPIRES_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*h$")


def _finite(d: dict, key: str, required: bool) -> float | None:
    v = d.get(key)
    if v is None:
        if required:
            raise IntentParseError(f"{key} is required")
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise IntentParseError(f"{key} must be a number") from None
    if not math.isfinite(f):
        raise IntentParseError(f"{key} must be finite")
    return f


def _enum(cls, d: dict, key: str):
    v = d.get(key)
    if v is None:
        raise IntentParseError(f"{key} is required")
    try:
        return cls(v)
    except ValueError:
        raise IntentParseError(
            f"invalid {key}: {v!r} (allowed: {[m.value for m in cls]})") from None


@dataclass(frozen=True, slots=True)
class TradeIntent:
    action: Action
    origin: Origin
    reasoning: str = ""
    pair: str | None = None
    direction: Direction | None = None
    entry_type: EntryType | None = None
    horizon: Horizon | None = None
    order_id: int | None = None
    limit_price: float | None = None
    expires_in_h: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float | None = None
    # レビュー修正 (codex 5): 判断時点の参照価格。LLM が出す値ではなく、
    # システムが Mission 開始時の quote から供給する (from_llm_dict は d から
    # 読まずキーワード引数で受け取る)。market intent のスリッページ判定に使う
    # (risk_gate.evaluate)。None ならスリッページ検証はスキップされる。
    ref_price: float | None = None

    @classmethod
    def from_llm_dict(cls, d: dict, *, origin: Origin,
                      ref_price: float | None = None) -> "TradeIntent":
        if not isinstance(d, dict):
            raise IntentParseError("intent must be an object")
        action = _enum(Action, d, "action")
        reasoning = str(d.get("reasoning", ""))

        if action in (Action.CLOSE, Action.CANCEL):
            order_id = d.get("order_id")
            if type(order_id) is not int or order_id <= 0:
                raise IntentParseError(
                    f"{action.value} requires a positive integer order_id, "
                    f"got {order_id!r}")
            return cls(action=action, origin=origin, order_id=order_id,
                       reasoning=reasoning, ref_price=ref_price)

        if action is Action.HOLD:
            return cls(action=action, origin=origin, reasoning=reasoning,
                       ref_price=ref_price)

        # action == open
        pair = d.get("pair")
        if not pair or not isinstance(pair, str):
            raise IntentParseError("open requires pair")
        direction = _enum(Direction, d, "direction")
        entry_type = _enum(EntryType, d, "entry_type")
        horizon = _enum(Horizon, d, "horizon")
        stop_loss = _finite(d, "stop_loss", required=True)
        take_profit = _finite(d, "take_profit", required=False)
        confidence = _finite(d, "confidence", required=False)

        limit_price = _finite(d, "limit_price", required=entry_type is EntryType.LIMIT)
        expires_in_h: float | None = None
        if entry_type is EntryType.LIMIT:
            raw = d.get("expires_in")
            if raw is None:
                raise IntentParseError("limit requires expires_in")
            m = _EXPIRES_RE.match(str(raw).strip())
            if not m:
                raise IntentParseError(f"expires_in must look like '4h', got {raw!r}")
            expires_in_h = float(m.group(1))
        else:
            if d.get("limit_price") is not None or d.get("expires_in") is not None:
                raise IntentParseError("market must not carry limit fields")
            limit_price = None

        return cls(action=action, origin=origin, pair=pair, direction=direction,
                   entry_type=entry_type, horizon=horizon, limit_price=limit_price,
                   expires_in_h=expires_in_h, stop_loss=stop_loss,
                   take_profit=take_profit, confidence=confidence,
                   reasoning=reasoning, ref_price=ref_price)


@dataclass(frozen=True, slots=True)
class Signal:
    """signal plugin (`detect(df, params)`) の 1 件の検出出力 (プラン 7 §6)。

    `bar_ts` は plugin の任意値ではなく、評価対象バケットからハーネス
    (signal_producer, プラン 7 Task 8) が設定する監査キー — plugin 側の
    `detect()` はこのフィールドを持たない dict を返し、ハーネスが
    `Signal` へ組み立てる際に付与する (鮮度ゲート・重複排除の前提)。
    """
    plugin: str
    pair: str
    timeframe: str
    bar_ts: datetime
    direction: Direction
    strength: float
    rationale: str
    stop_loss: float | None = None
    take_profit: float | None = None

    def __post_init__(self) -> None:
        if self.direction not in (Direction.LONG, Direction.SHORT):
            raise ValueError(f"invalid direction: {self.direction!r}")
        if isinstance(self.strength, bool) or not isinstance(self.strength, (int, float)):
            raise ValueError(f"strength must be a number, got {self.strength!r}")
        if not math.isfinite(self.strength) or not (0.0 <= self.strength <= 1.0):
            raise ValueError(f"strength must be in [0, 1], got {self.strength!r}")


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """strategy plugin (`evaluate(df, indicators, signals, params)`) の出力
    (プラン 7 §6)。exit 表現は levels (stop_loss/take_profit) のみ — D1。
    action="open" は stop_loss 必須 (帰属規則: ハーネスの既定決済で補わない)。

    フィールド順は「デフォルト無し → 有り」の dataclass 制約により
    action, rationale (共に必須) を先頭に置く (仕様書の記載順はこの制約を
    明示していないため、意味は保ったまま並べ替えている)。
    """
    action: StrategyAction
    rationale: str
    direction: Direction | None = None
    entry_type: EntryType | None = None
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None

    def __post_init__(self) -> None:
        if self.action not in (StrategyAction.OPEN, StrategyAction.HOLD):
            raise ValueError(f"invalid action: {self.action!r}")
        if self.direction is not None and self.direction not in (
                Direction.LONG, Direction.SHORT):
            raise ValueError(f"invalid direction: {self.direction!r}")
        if self.entry_type is not None and self.entry_type not in (
                EntryType.MARKET, EntryType.LIMIT):
            raise ValueError(f"invalid entry_type: {self.entry_type!r}")

        if self.action == StrategyAction.OPEN:
            # brief 明記: open は stop_loss 必須。direction/entry_type は
            # brief に明記が無いが、方向・執行方法の無い "open" は下流
            # (Task 5 strategy_adapter の IntentSource 変換) で意味を成さない
            # ため fail closed で必須化する。
            if self.stop_loss is None:
                raise ValueError("open requires stop_loss")
            if self.direction is None:
                raise ValueError("open requires direction")
            if self.entry_type is None:
                raise ValueError("open requires entry_type")

        if self.entry_type == EntryType.LIMIT and self.limit_price is None:
            raise ValueError("limit entry_type requires limit_price")
        if self.entry_type != EntryType.LIMIT and self.limit_price is not None:
            raise ValueError("limit_price is only valid when entry_type is limit")
