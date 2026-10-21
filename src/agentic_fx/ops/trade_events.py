"""取引 event (`order_state_changed` / `position_closed`) の値の書式・検査と、遷移後の行からの組み立て。

値は field ごとの固定書式の文字列にする。grammar に合うだけでは通さず、parse して同じ書式で
作り直した文字列が元と完全に一致する値 (canonical) だけを通す。afx-ops 側の
`afx_ops/trade_events.py` は同じ grammar を持ち、2 つの package の一致は test で固定する。
通らない値があるときは event を作らない (None に置き換えない)。
この module は DB もファイルも読まない純粋関数だけを持つ。
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

ORDER_CODE = "order_state_changed"
CLOSE_CODE = "position_closed"
TRADE_CODES = frozenset({ORDER_CODE, CLOSE_CODE})

# sink が数える欠落の理由。`afx ctl status` の表示もこの並びを使う (名前は固定で文言は載せない)。
DROP_REASONS = ("queue_drop", "observer_error", "db_busy_drop", "db_error_drop",
                "invalid_value_drop", "worker_death", "shutdown_drop")

ORDER_FIELDS = frozenset({"state", "pair", "side", "entry_type", "qty", "price", "sl", "tp",
                          "origin_code", "mode", "state_mode", "reason_code"})
CLOSE_FIELDS = frozenset({"pair", "side", "qty", "entry_price", "close_price", "pnl", "ccy",
                          "pnl_r", "held_sec", "reason_code", "origin_code", "mode", "state_mode"})
FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {ORDER_CODE: ORDER_FIELDS, CLOSE_CODE: CLOSE_FIELDS})

ORDER_STATES = frozenset({"placed", "filled", "rejected", "cancelled", "expired"})

REF_PATTERN = re.compile(r"order:[0-9]{1,19}")
MAX_ORDER_ID = 2 ** 63 - 1

_PRICE = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,8})?")
_PNL = re.compile(r"-?[0-9]{1,15}\.[0-9]{2}")
_PNL_R = re.compile(r"-?[0-9]{1,6}\.[0-9]{2}")
_PAIR = re.compile(r"[A-Z0-9]{3,12}")
_CCY = re.compile(r"[A-Z]{3}")
_ORIGIN = re.compile(r"cron|signal:[a-z][a-z0-9_]{0,63}")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,31}")

_PRICE_LIMIT = Decimal(10) ** 12
_PNL_LIMIT = Decimal(10) ** 15
_PNL_R_LIMIT = Decimal(10) ** 6
_HELD_LIMIT = 10 ** 10
MAX_CHARS = 64


class InvalidValue(ValueError):
    """書式化した値が grammar を通らない。event を作らずに捨てる。"""


def _decimal(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _price_text(text: str) -> str:
    return text.rstrip("0").rstrip(".") if "." in text else text


def _money_text(text: str) -> str:
    return "0.00" if Decimal(text) == 0 else text


def _canonical_price(value: object) -> bool:
    if type(value) is not str or len(value) > MAX_CHARS or _PRICE.fullmatch(value) is None:
        return False
    parsed = _decimal(value)
    return (parsed is not None and 0 < parsed < _PRICE_LIMIT
            and _price_text(f"{parsed:.8f}") == value)


def _canonical_signed(pattern: re.Pattern[str], limit: Decimal) -> Callable[[object], bool]:
    def check(value: object) -> bool:
        if type(value) is not str or len(value) > MAX_CHARS or pattern.fullmatch(value) is None:
            return False
        parsed = _decimal(value)
        return (parsed is not None and abs(parsed) < limit
                and _money_text(f"{parsed:.2f}") == value)
    return check


def _enum(*members: str) -> Callable[[object], bool]:
    allowed = frozenset(members)
    return lambda value: type(value) is str and value in allowed


def _pattern(pattern: re.Pattern[str]) -> Callable[[object], bool]:
    return lambda value: type(value) is str and pattern.fullmatch(value) is not None


def _nullable(check: Callable[[object], bool]) -> Callable[[object], bool]:
    return lambda value: value is None or check(value)


def _held_sec(value: object) -> bool:
    return type(value) is int and 0 <= value < _HELD_LIMIT


_price = _canonical_price
_pnl = _canonical_signed(_PNL, _PNL_LIMIT)
_pnl_r = _canonical_signed(_PNL_R, _PNL_R_LIMIT)
_origin_form = _pattern(_ORIGIN)
# `signal:` を付けて値の上限に収まらないものは、組み立てる側が None にする
_origin = _nullable(lambda value: _origin_form(value) and len(value) <= MAX_CHARS)
_state_mode = _nullable(_enum("learning", "trading"))
_side = _enum("long", "short")
_mode = _enum("paper", "live")
_reason = _pattern(_REASON)

FIELD_CHECKS: Mapping[str, Mapping[str, Callable[[object], bool]]] = MappingProxyType({
    ORDER_CODE: MappingProxyType({
        "state": _enum(*sorted(ORDER_STATES)), "pair": _pattern(_PAIR), "side": _side,
        "entry_type": _enum("market", "limit"), "qty": _price, "price": _price, "sl": _price,
        "tp": _nullable(_price), "origin_code": _origin, "mode": _mode,
        "state_mode": _state_mode, "reason_code": _nullable(_reason),
    }),
    CLOSE_CODE: MappingProxyType({
        "pair": _pattern(_PAIR), "side": _side, "qty": _price, "entry_price": _price,
        "close_price": _price, "pnl": _nullable(_pnl), "ccy": _pattern(_CCY),
        "pnl_r": _nullable(_pnl_r), "held_sec": _nullable(_held_sec), "reason_code": _reason,
        "origin_code": _origin, "mode": _mode, "state_mode": _state_mode,
    }),
})


def valid_ref(ref: object) -> bool:
    return (type(ref) is str and REF_PATTERN.fullmatch(ref) is not None
            and 1 <= int(ref[len("order:"):]) <= MAX_ORDER_ID)


def valid_fields(code: str, fields: object) -> bool:
    """field 集合の完全一致と、各値の grammar・範囲・canonical を全て満たすか。"""
    checks = FIELD_CHECKS[code]
    if not (type(fields) is dict and set(fields) == set(checks)
            and all(check(fields[name]) for name, check in checks.items())):
        return False
    # 注文の reason_code は取消のときだけ持てる。他の状態では必ず None
    return code != ORDER_CODE or fields["state"] == "cancelled" or fields["reason_code"] is None


# ---- 書式化 (producer) ----------------------------------------------------


def format_price(value: object) -> str:
    """価格・数量。8 小数で書式化し末尾の 0 と小数点を落とす。通らなければ InvalidValue。"""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise InvalidValue("price")
    text = _price_text(f"{value:.8f}")
    if not _canonical_price(text):
        raise InvalidValue("price")
    return text


def _format_signed(value: object, digits: int, pattern: re.Pattern[str],
                   limit: Decimal) -> str | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise InvalidValue("signed")
    text = _money_text(f"{value:.{digits}f}")
    if len(text) > MAX_CHARS or pattern.fullmatch(text) is None or abs(Decimal(text)) >= limit:
        raise InvalidValue("signed")
    return text


def format_money(value: object) -> str | None:
    """口座通貨の損益 (2 小数、`-0.00` は `0.00`)。元が None なら None。"""
    return _format_signed(value, 2, _PNL, _PNL_LIMIT)


def format_r(value: object) -> str | None:
    return _format_signed(value, 2, _PNL_R, _PNL_R_LIMIT)


def origin_code(trigger: object) -> str | None:
    """`missions.trigger` を境界で再検査する。形が合わない、または収まらなければ None。"""
    if type(trigger) is str and _origin_form(trigger) and len(trigger) <= MAX_CHARS:
        return trigger
    return None


def held_seconds(filled_at: object, closed_at: object) -> int | None:
    """保有秒。どちらかが NULL なら None。NULL でないのに読めない値は InvalidValue。"""
    if filled_at is None or closed_at is None:
        return None
    if type(filled_at) is not str or type(closed_at) is not str:
        raise InvalidValue("held_sec")
    try:
        delta = datetime.fromisoformat(closed_at) - datetime.fromisoformat(filled_at)
    except ValueError:
        raise InvalidValue("held_sec") from None
    except TypeError:
        return None  # naive と aware の混在は比べられない
    return max(0, int(delta.total_seconds()))


def _pnl_r_text(row: Mapping[str, object]) -> str | None:
    """R 倍数の書式。grammar に収まらない (桁あふれ等) ときは R だけを None にする。"""
    try:
        return format_r(_r_multiple(row))
    except InvalidValue:
        return None


def _r_multiple(row: Mapping[str, object]) -> float | None:
    entry, close, stop = row["avg_fill_price"], row["close_price"], row["stop_loss"]
    if None in (entry, close, stop) or row["direction"] not in ("long", "short"):
        return None
    risk = abs(entry - stop)
    if risk == 0:
        return None
    sign = 1.0 if row["direction"] == "long" else -1.0
    return (close - entry) * sign / risk


# 遷移先 (orders.status) → 発注 event の state
_STATE_OF = {"pending_fill": "placed", "open": "filled", "rejected": "rejected",
             "cancelled": "cancelled", "expired": "expired"}


def has_event(to_status: str) -> bool:
    """この遷移先に event があるか (途中状態は無い)。"""
    return to_status == "closed" or to_status in _STATE_OF


def build_event(row: Mapping[str, object], to_status: str, *, trigger: object,
                state_mode: str | None, ccy: str,
                mode: str = "paper") -> tuple[str, str, dict] | None:
    """遷移後の orders 行から (code, ref, fields) を作る。event の無い遷移先は None。

    読む列は §2.4 の一覧だけ。値が grammar を通らないときは InvalidValue。
    """
    if not has_event(to_status):
        return None
    ref = f"order:{row['id']}"
    origin = origin_code(trigger)
    if to_status == "closed":
        fields = {
            "pair": row["pair"], "side": row["direction"],
            "qty": format_price(row["quantity"]),
            "entry_price": format_price(row["avg_fill_price"]),
            "close_price": format_price(row["close_price"]),
            "pnl": format_money(row["realized_pnl"]), "ccy": ccy,
            "pnl_r": _pnl_r_text(row),
            "held_sec": held_seconds(row["filled_at"], row["closed_at"]),
            "reason_code": row["close_reason"], "origin_code": origin,
            "mode": mode, "state_mode": state_mode,
        }
        code = CLOSE_CODE
    else:
        state = _STATE_OF[to_status]
        price = row["avg_fill_price"] if state == "filled" else row["requested_price"]
        tp = row["take_profit"]
        fields = {
            "state": state, "pair": row["pair"], "side": row["direction"],
            "entry_type": row["entry_type"], "qty": format_price(row["quantity"]),
            "price": format_price(price), "sl": format_price(row["stop_loss"]),
            "tp": None if tp is None else format_price(tp),
            "origin_code": origin, "mode": mode, "state_mode": state_mode,
            "reason_code": row["close_reason"] if state == "cancelled" else None,
        }
        code = ORDER_CODE
    if not valid_ref(ref) or not valid_fields(code, fields):
        raise InvalidValue(code)
    return code, ref, fields
