"""取引 event (`order_state_changed` / `position_closed`) の Discord 文面。

固定 template に、完全検証を通った値だけを埋める。event から自由文は復元しない。非信頼値
(`origin_code`、`pair`) は escape して置く。数値は event の桁をそのまま保ち、桁は足さない
(桁区切りと符号だけを付ける)。設定済みの mention は付けない (呼び側は allowed_mentions を none にする)。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from afx_ops.trade_events import CLOSE_CODE, ORDER_CODE

from .render import escape_discord

_STATE_TITLES = {
    "placed": "指値を入れました",
    "filled": "約定しました",
    "rejected": "発注が拒否されました",
    "cancelled": "指値を取り消しました",
    "expired": "指値が失効しました",
}
_SIDES = {"long": "買い", "short": "売り"}
_PREFIX = {"paper": "[ペーパー]", "live": "[実発注]"}
_REASONS = {
    "sl": "損切り到達",
    "tp": "利確到達",
    "day_rollover": "日次の強制決済",
    "llm_close": "判断による決済",
    "llm_cancel": "判断による取消",
    "market_close": "市場クローズ前の取消",
    "reservation": "予約を維持できない",
    "rate_unavailable": "換算レート不明",
    "account_unknown": "口座情報なし",
    "equity_nonpositive": "残高が正でない",
}
UNKNOWN_REASON = "理由は表示できません"
AMOUNTS = ("absolute", "relative")
# 通貨の minor unit が 0 のもの。損益は整数に四捨五入して出す
_ZERO_DECIMAL_CCY = frozenset({"JPY"})


def _group(text: str) -> str:
    """整数部に桁区切りを付ける。小数の桁は触らない。"""
    sign = "-" if text.startswith("-") else ""
    whole, dot, frac = text.lstrip("-").partition(".")
    return f"{sign}{int(whole):,}{dot}{frac}"


def _signed(text: str) -> str:
    """先頭に符号を付ける (正は +、負は - のまま、0 は付けない)。"""
    value = Decimal(text)
    return f"+{text}" if value > 0 else text


def _pnl_text(pnl: str, ccy: str) -> str:
    if ccy in _ZERO_DECIMAL_CCY:
        rounded = Decimal(pnl).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        text = str(int(rounded))
    else:
        text = pnl
    return f"{'+' if Decimal(text) > 0 else ''}{_group(text)} {ccy}"


def _percent(side: str, entry: str, close: str) -> str:
    direction = 1 if side == "long" else -1
    change = (Decimal(close) - Decimal(entry)) * direction / Decimal(entry) * 100
    value = change.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = "0.00" if value == 0 else f"{value:.2f}"
    return f"{'+' if value > 0 else ''}{text}%"


def _held(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 日 {hours} 時間"
    if hours:
        return f"{hours} 時間 {minutes} 分"
    if minutes:
        return f"{minutes} 分"
    return f"{seconds} 秒"


def _reason(code: object) -> str:
    return _REASONS.get(code, UNKNOWN_REASON) if type(code) is str else UNKNOWN_REASON


def _head(payload: dict, title: str) -> str:
    prefix = _PREFIX[payload["mode"]]
    if payload["state_mode"] == "learning":
        prefix += " 学習中"
    order_id = int(payload["ref"].split(":", 1)[1])
    return (f"{prefix} {title} #{order_id} {escape_discord(payload['pair'])} "
            f"{_SIDES[payload['side']]}")


def _origin_line(payload: dict) -> list[str]:
    origin = payload["origin_code"]
    return [] if origin is None else [f"起点 {escape_discord(origin)}"]


def _render_order(payload: dict, amounts: str) -> str:
    head = _head(payload, _STATE_TITLES[payload["state"]])
    price = _group(payload["price"])
    if payload["state"] in ("placed", "filled"):
        first = f"数量 {_group(payload['qty'])} lot / 価格 {price}" if amounts == "absolute" \
            else f"価格 {price}"
        tp = "なし" if payload["tp"] is None else _group(payload["tp"])
        return "\n".join([head, first, f"SL {_group(payload['sl'])} / TP {tp}",
                          *_origin_line(payload)])
    qty = f" {_group(payload['qty'])} lot" if amounts == "absolute" else ""
    reason = payload["reason_code"]
    tail = "" if reason is None else f" ({_reason(reason)})"
    return f"{head}{qty} @{price}{tail}"


def _render_close(payload: dict, amounts: str) -> str:
    head = _head(payload, "決済しました") + f" ({_reason(payload['reason_code'])})"
    prices = f"決済価格 {_group(payload['close_price'])} (約定 {_group(payload['entry_price'])})"
    r_text = None if payload["pnl_r"] is None else f"{_signed(payload['pnl_r'])}R"
    if amounts == "absolute":
        prices += f" / 数量 {_group(payload['qty'])} lot"
        amount = "未確定" if payload["pnl"] is None else _pnl_text(payload["pnl"], payload["ccy"])
        result = amount + ("" if r_text is None else f" ({r_text})")
    else:
        percent = f"価格 {_percent(payload['side'], payload['entry_price'], payload['close_price'])}"
        result = percent if r_text is None else f"{r_text} ({percent})"
    held = payload["held_sec"]
    line = f"損益 {result}" + ("" if held is None else f" / 保有 {_held(held)}")
    return "\n".join([head, prices, line, *_origin_line(payload)])


def render_trade(payload: dict, *, amounts: str = "absolute") -> str:
    """完全検証を通った取引 event の射影を文面にする。"""
    if amounts not in AMOUNTS:
        raise ValueError("amounts")
    if payload["code"] == ORDER_CODE:
        return _render_order(payload, amounts)
    if payload["code"] == CLOSE_CODE:
        return _render_close(payload, amounts)
    raise ValueError("code")
