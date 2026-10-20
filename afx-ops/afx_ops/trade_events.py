"""取引 event (`order_state_changed` / `position_closed`) の field 集合と値の grammar。

値は daemon 側が field ごとの固定書式の文字列にして出す。ここは受け側で同じ書式を再検査する:
grammar に合うだけでは受理せず、parse して同じ formatter で作り直した文字列が元の文字列と完全に
一致する値だけを通す (`0001.20` や `1.50` のような producer が作らない書式は拒否する)。
通らない event は全体を拒否する (項目だけを省かない)。
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

ORDER_CODE = "order_state_changed"
CLOSE_CODE = "position_closed"
TRADE_CODES = frozenset({ORDER_CODE, CLOSE_CODE})

ORDER_FIELDS = frozenset({"state", "pair", "side", "entry_type", "qty", "price", "sl", "tp",
                          "origin_code", "mode", "state_mode", "reason_code"})
CLOSE_FIELDS = frozenset({"pair", "side", "qty", "entry_price", "close_price", "pnl", "ccy",
                          "pnl_r", "held_sec", "reason_code", "origin_code", "mode", "state_mode"})

ORDER_STATES = frozenset({"placed", "filled", "rejected", "cancelled", "expired"})

# order:<orders.id>。regex の後に整数化して範囲を見る (0 と 2^63 以上を通さない)
REF_PATTERN = re.compile(r"order:[0-9]{1,19}\Z")
MAX_ORDER_ID = 2 ** 63 - 1

_PRICE = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,8})?\Z")
_PNL = re.compile(r"-?[0-9]{1,15}\.[0-9]{2}\Z")
_PNL_R = re.compile(r"-?[0-9]{1,6}\.[0-9]{2}\Z")
_PAIR = re.compile(r"[A-Z0-9]{3,12}\Z")
_CCY = re.compile(r"[A-Z]{3}\Z")
_ORIGIN = re.compile(r"(?:cron|signal:[a-z][a-z0-9_]{0,63})\Z")
# reason_code は列挙でなく安全な語の形だけを見る。未知の語は表示側が汎用文にする
_REASON = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")

_PRICE_LIMIT = Decimal(10) ** 12
_PNL_LIMIT = Decimal(10) ** 15
_PNL_R_LIMIT = Decimal(10) ** 6
_HELD_LIMIT = 10 ** 10
_MAX_CHARS = 64


def _decimal(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def format_price(value: Decimal) -> str:
    """価格・数量の固定書式 (8 小数、末尾の 0 と小数点を落とす。指数表記を使わない)。"""
    text = f"{value:.8f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_money(value: Decimal) -> str:
    """損益の固定書式 (2 小数)。`-0.00` は `0.00` にする。"""
    text = f"{value:.2f}"
    return "0.00" if Decimal(text) == 0 else text


def _canonical_price(value: object) -> bool:
    if type(value) is not str or len(value) > _MAX_CHARS or _PRICE.fullmatch(value) is None:
        return False
    parsed = _decimal(value)
    return (parsed is not None and 0 < parsed < _PRICE_LIMIT
            and format_price(parsed) == value)


def _canonical_signed(pattern: re.Pattern[str], limit: Decimal) -> Callable[[object], bool]:
    def check(value: object) -> bool:
        if type(value) is not str or len(value) > _MAX_CHARS or pattern.fullmatch(value) is None:
            return False
        parsed = _decimal(value)
        return parsed is not None and abs(parsed) < limit and format_money(parsed) == value
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
_pair = _pattern(_PAIR)
_origin_form = _pattern(_ORIGIN)
# `signal:` を付けて event の値の上限に収まらないものは、producer が None にする
_origin = _nullable(lambda value: _origin_form(value) and len(value) <= _MAX_CHARS)
_mode = _enum("paper", "live")
_state_mode = _nullable(_enum("learning", "trading"))
_side = _enum("long", "short")
_reason = _pattern(_REASON)

FIELD_CHECKS: Mapping[str, Mapping[str, Callable[[object], bool]]] = MappingProxyType({
    ORDER_CODE: MappingProxyType({
        "state": _enum(*sorted(ORDER_STATES)), "pair": _pair, "side": _side,
        "entry_type": _enum("market", "limit"), "qty": _price, "price": _price, "sl": _price,
        "tp": _nullable(_price), "origin_code": _origin, "mode": _mode,
        "state_mode": _state_mode, "reason_code": _nullable(_reason),
    }),
    CLOSE_CODE: MappingProxyType({
        "pair": _pair, "side": _side, "qty": _price, "entry_price": _price,
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
    return (type(fields) is dict and set(fields) == set(checks)
            and all(check(fields[name]) for name, check in checks.items()))
