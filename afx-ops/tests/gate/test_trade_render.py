"""取引 event の Discord 文面 (AC-20、AC-22)。固定 template に検査済みの値だけを埋める。"""
from __future__ import annotations

import pytest

from afx_ops.gate.trade_render import render_trade
from afx_ops.notify.payload import encode_event

TS = "2026-10-20T00:00:00+00:00"


def _order(state: str = "placed", **fields) -> dict:
    base = {"state": state, "pair": "USDJPY", "side": "long", "entry_type": "limit",
            "qty": "0.1", "price": "150.123", "sl": "149.8", "tp": "150.9",
            "origin_code": "signal:rsi_div", "mode": "paper", "state_mode": "learning",
            "reason_code": None}
    base.update(fields)
    return encode_event({"id": 7, "ts": TS, "code": "order_state_changed", "ref": "order:12",
                         "fields": base})


def _close(**fields) -> dict:
    base = {"pair": "USDJPY", "side": "long", "qty": "0.1", "entry_price": "150.123",
            "close_price": "150.9", "pnl": "7770.00", "ccy": "JPY", "pnl_r": "2.40",
            "held_sec": 11520, "reason_code": "tp", "origin_code": "signal:rsi_div",
            "mode": "paper", "state_mode": None}
    base.update(fields)
    return encode_event({"id": 8, "ts": TS, "code": "position_closed", "ref": "order:12",
                         "fields": base})


def test_ac20_placed_golden_with_the_learning_marker():
    assert render_trade(_order("placed")) == (
        "[ペーパー] 学習中 指値を入れました #12 USDJPY 買い\n"
        "数量 0.1 lot / 価格 150.123\n"
        "SL 149.8 / TP 150.9\n"
        "起点 signal\\:rsi\\_div")


def test_ac20_filled_golden_swaps_the_title_only():
    assert render_trade(_order("filled", side="short", state_mode=None, tp=None)) == (
        "[ペーパー] 約定しました #12 USDJPY 売り\n"
        "数量 0.1 lot / 価格 150.123\n"
        "SL 149.8 / TP なし\n"
        "起点 signal\\:rsi\\_div")


def test_ac20_closed_absolute_golden_for_jpy_rounds_to_an_integer():
    assert render_trade(_close(pnl="7770.40")) == (
        "[ペーパー] 決済しました #12 USDJPY 買い (利確到達)\n"
        "決済価格 150.9 (約定 150.123) / 数量 0.1 lot\n"
        "損益 +7,770 JPY (+2.40R) / 保有 3 時間 12 分\n"
        "起点 signal\\:rsi\\_div")


def test_ac20_closed_relative_golden_has_no_quantity_or_amount():
    text = render_trade(_close(qty="0.37"), amounts="relative")
    assert text == (
        "[ペーパー] 決済しました #12 USDJPY 買い (利確到達)\n"
        "決済価格 150.9 (約定 150.123)\n"
        "損益 +2.40R (価格 +0.52%) / 保有 3 時間 12 分\n"
        "起点 signal\\:rsi\\_div")
    for hidden in ("0.37", "7770", "7,770", " JPY", "lot", "数量"):
        assert hidden not in text


@pytest.mark.parametrize("state,title,reason,tail", [
    ("rejected", "発注が拒否されました", None, ""),
    ("cancelled", "指値を取り消しました", "market_close", " (市場クローズ前の取消)"),
    ("expired", "指値が失効しました", None, ""),
])
def test_ac20_terminal_order_states_are_one_line(state, title, reason, tail):
    event = _order(state, reason_code=reason)
    assert render_trade(event) == (
        f"[ペーパー] 学習中 {title} #12 USDJPY 買い 0.1 lot @150.123{tail}")
    assert render_trade(event, amounts="relative") == (
        f"[ペーパー] 学習中 {title} #12 USDJPY 買い @150.123{tail}")


def test_ac20_live_mode_uses_the_live_prefix_and_none_state_mode_has_no_marker():
    assert render_trade(_order("filled", mode="live", state_mode=None)).splitlines()[0] == (
        "[実発注] 約定しました #12 USDJPY 買い")
    assert render_trade(_close(mode="live", state_mode="trading")).splitlines()[0] == (
        "[実発注] 決済しました #12 USDJPY 買い (利確到達)")


def test_ac20_origin_line_is_omitted_when_none():
    assert "起点" not in render_trade(_order("placed", origin_code=None))
    assert "起点" not in render_trade(_close(origin_code=None))
    assert render_trade(_order("placed", origin_code="cron")).splitlines()[-1] == "起点 cron"


def test_ac20_pnl_none_is_unsettled_and_r_none_is_omitted():
    lines = render_trade(_close(pnl=None)).splitlines()
    assert lines[2] == "損益 未確定 (+2.40R) / 保有 3 時間 12 分"
    lines = render_trade(_close(pnl=None, pnl_r=None)).splitlines()
    assert lines[2] == "損益 未確定 / 保有 3 時間 12 分"
    lines = render_trade(_close(pnl_r=None)).splitlines()
    assert lines[2] == "損益 +7,770 JPY / 保有 3 時間 12 分"


def test_ac20_held_sec_none_drops_the_holding_part():
    assert render_trade(_close(held_sec=None)).splitlines()[2] == "損益 +7,770 JPY (+2.40R)"


@pytest.mark.parametrize("seconds,shown", [
    (0, "0 秒"), (59, "59 秒"), (60, "1 分"), (3599, "59 分"), (3600, "1 時間 0 分"),
    (11520, "3 時間 12 分"), (86399, "23 時間 59 分"), (90000, "1 日 1 時間"),
    (10 ** 10 - 1, "115740 日 17 時間")])
def test_ac20_holding_time_formats(seconds, shown):
    assert render_trade(_close(held_sec=seconds)).splitlines()[2].endswith(f"/ 保有 {shown}")


def test_ac20_jpy_pnl_rounds_half_up_to_an_integer_and_others_keep_two_decimals():
    assert "損益 -1,235 JPY" in render_trade(_close(pnl="-1234.50", pnl_r=None))
    assert "損益 +1,235 JPY" in render_trade(_close(pnl="1234.50", pnl_r=None))
    assert "損益 0 JPY" in render_trade(_close(pnl="-0.40", pnl_r=None))
    assert "損益 0 JPY" in render_trade(_close(pnl="0.00", pnl_r=None))
    assert "損益 +77.70 USD" in render_trade(_close(pnl="77.70", ccy="USD", pnl_r=None))
    assert "損益 -1,000,000.05 USD" in render_trade(_close(pnl="-1000000.05", ccy="USD",
                                                           pnl_r=None))
    assert "損益 0.00 USD" in render_trade(_close(pnl="0.00", ccy="USD", pnl_r=None))


def test_ac20_r_multiple_keeps_the_two_digit_event_value_with_a_sign():
    assert "(-1.00R)" in render_trade(_close(pnl_r="-1.00"))
    assert "(0.00R)" in render_trade(_close(pnl_r="0.00"))


def test_ac20_relative_percent_is_a_decimal_calculation_with_the_side_sign():
    def line(**fields) -> str:
        return render_trade(_close(**fields), amounts="relative").splitlines()[2]

    assert line(entry_price="100", close_price="101.5", pnl_r=None).startswith("損益 価格 +1.50%")
    assert line(side="short", entry_price="100", close_price="101.5").startswith(
        "損益 +2.40R (価格 -1.50%)")
    assert line(side="short", entry_price="100", close_price="99").startswith(
        "損益 +2.40R (価格 +1.00%)")
    # 丸めは 0.5 を切り上げる (float の 2 進誤差に左右されない)
    assert "価格 +0.01%" in line(entry_price="10000", close_price="10000.5")
    assert "価格 0.00%" in line(entry_price="10000", close_price="10000.4")
    assert "価格 0.00%" in line(side="short", entry_price="10000", close_price="10000.4")
    assert "価格 -0.01%" in line(entry_price="10000", close_price="9999.5")


def test_ac20_relative_percent_does_not_depend_on_pnl_being_known():
    text = render_trade(_close(pnl=None, pnl_r=None), amounts="relative")
    assert text.splitlines()[2] == "損益 価格 +0.52% / 保有 3 時間 12 分"
    assert "未確定" not in text


def test_ac20_an_unknown_reason_code_gets_a_fixed_generic_sentence_without_the_raw_value():
    text = render_trade(_close(reason_code="brand_new"))
    assert "(" in text.splitlines()[0] and "brand_new" not in text
    assert text.splitlines()[0] == "[ペーパー] 決済しました #12 USDJPY 買い (理由は表示できません)"
    cancelled = render_trade(_order("cancelled", reason_code="zz_new"))
    assert "zz_new" not in cancelled and cancelled.endswith("(理由は表示できません)")


@pytest.mark.parametrize("code,text", [
    ("sl", "損切り到達"), ("tp", "利確到達"), ("day_rollover", "日次の強制決済"),
    ("llm_close", "判断による決済")])
def test_ac20_close_reason_table(code, text):
    assert render_trade(_close(reason_code=code)).splitlines()[0].endswith(f"({text})")


@pytest.mark.parametrize("code,text", [
    ("llm_cancel", "判断による取消"), ("market_close", "市場クローズ前の取消"),
    ("reservation", "予約を維持できない"), ("rate_unavailable", "換算レート不明"),
    ("account_unknown", "口座情報なし"), ("equity_nonpositive", "残高が正でない")])
def test_ac20_cancel_reason_table(code, text):
    assert render_trade(_order("cancelled", reason_code=code)).endswith(f"({text})")


def test_ac22_the_posted_text_escapes_untrusted_looking_values():
    text = render_trade(_order("placed", origin_code="signal:a_b"))
    assert "signal\\:a\\_b" in text
    assert "@" not in text and "<" not in text
    # 本文に現れる @ は価格の前の記号だけで、mention 記法は現れない
    for event in (_order("filled"), _close(), _order("cancelled", reason_code="llm_cancel")):
        body = render_trade(event)
        assert "<" not in body and "@everyone" not in body and "@here" not in body
        assert all(body[i + 1].isdigit() for i, ch in enumerate(body) if ch == "@")


def test_ac20_a_text_stays_within_eight_lines_and_two_thousand_chars():
    for event in (_order("placed"), _close(pnl="-1000000000000.00", ccy="USD")):
        text = render_trade(event)
        assert len(text.splitlines()) <= 8 and len(text) <= 2000
