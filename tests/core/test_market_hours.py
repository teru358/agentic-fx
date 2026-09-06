from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.market_hours import (
    is_friday_after, is_market_open, next_rollover, trading_day_start,
)


def _dt(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_weekend_closed():
    assert is_market_open(_dt(2026, 7, 22, 12, 0)) is True    # 水曜
    assert is_market_open(_dt(2026, 7, 24, 20, 59)) is True   # 金 20:59
    assert is_market_open(_dt(2026, 7, 24, 21, 0)) is False   # 金 21:00
    assert is_market_open(_dt(2026, 7, 25, 12, 0)) is False   # 土
    assert is_market_open(_dt(2026, 7, 26, 20, 59)) is False  # 日 20:59
    assert is_market_open(_dt(2026, 7, 26, 21, 0)) is True    # 日 21:00


def test_trading_day_start():
    assert trading_day_start(_dt(2026, 7, 22, 12, 0)) == _dt(2026, 7, 21, 21, 0)
    assert trading_day_start(_dt(2026, 7, 22, 22, 0)) == _dt(2026, 7, 22, 21, 0)


def test_next_rollover():
    assert next_rollover(_dt(2026, 7, 22, 12, 0)) == _dt(2026, 7, 22, 21, 0)
    assert next_rollover(_dt(2026, 7, 22, 21, 0)) == _dt(2026, 7, 23, 21, 0)


def test_friday_cutoff():
    """cutoff は NY 現地時間の hh:mm (friday_swing_cutoff_ny)。
    夏 (EDT, UTC-4) では 18:00 UTC = NY 14:00 なので、cutoff "14:00" は
    旧 UTC 固定 "18:00" と夏時間においては同じ境界になる。"""
    assert is_friday_after(_dt(2026, 7, 24, 18, 30), "14:00") is True   # NY 14:30
    assert is_friday_after(_dt(2026, 7, 24, 17, 59), "14:00") is False  # NY 13:59
    assert is_friday_after(_dt(2026, 7, 23, 19, 0), "14:00") is False   # 木曜 (NY 15:00 木)


def test_timezone_normalization():
    """非 UTC の tz-aware datetime を渡した場合、UTC に正規化して判定する"""
    jst = timezone(timedelta(hours=9))
    # 2026-07-25 05:00 JST = 2026-07-24 20:00 UTC (金曜 20:00、市場 open)
    jst_time = datetime(2026, 7, 25, 5, 0, tzinfo=jst)
    utc_time = datetime(2026, 7, 24, 20, 0, tzinfo=timezone.utc)

    # 同じ瞬間でも JST では土曜日が壁時計だが、UTC では金曜 20:00
    # → UTC に正規化すれば True (市場 open)
    assert is_market_open(jst_time) is True
    assert is_market_open(utc_time) is True

    # trading_day_start も同じ瞬間なら同じ UTC 時刻を返す
    # 20:00 は 21:00 より前なので、前日（2026-07-23）のロールオーバーを返す
    assert trading_day_start(jst_time) == _dt(2026, 7, 23, 21, 0)
    assert trading_day_start(utc_time) == _dt(2026, 7, 23, 21, 0)

    # next_rollover も同じ
    assert next_rollover(jst_time) == _dt(2026, 7, 24, 21, 0)
    assert next_rollover(utc_time) == _dt(2026, 7, 24, 21, 0)

    # is_friday_after: JST では土曜、UTC では金曜、NY (夏時間) では金曜 16:00
    # → いずれの表現でも同一の瞬間なので、NY 現地時間に正規化して判定すれば
    #   金曜 16:00 は "21:00" cutoff 前なので False
    assert is_friday_after(jst_time, "21:00") is False
    assert is_friday_after(utc_time, "21:00") is False


# --- OANDA Japan MT5 実測: 夏冬とも 21:00 UTC 固定 ---

def test_winter_weekend_boundary_is_fixed_at_21_00_utc():
    """M1/M2: 冬も金曜 close / 日曜 open は 21:00 UTC 固定。"""
    fri_open = _dt(2026, 1, 16, 20, 55)
    fri_close = _dt(2026, 1, 16, 21, 0)
    assert is_market_open(fri_open) is True
    assert is_market_open(fri_close) is False
    assert is_market_open(_dt(2026, 1, 18, 20, 55)) is False
    assert is_market_open(_dt(2026, 1, 18, 21, 0)) is True


def test_utc_holidays_are_closed():
    """M3: ブローカー休場日の 12/25 と 1/1 は曜日に関係なく閉場。"""
    assert is_market_open(_dt(2025, 12, 25, 12, 0)) is False
    assert is_market_open(_dt(2026, 1, 1, 12, 0)) is False


def test_holiday_follows_trading_day_label_not_utc_calendar_day():
    """codex 1 周目 [重要]: 祝日は取引日ラベル (21:00 UTC 起点) で判定する。
    実測: 12/24 21:00 UTC 閉場 → 12/25 21:00 UTC 再開 (1/1 も同型)。"""
    assert is_market_open(_dt(2025, 12, 24, 20, 55)) is True
    assert is_market_open(_dt(2025, 12, 24, 21, 0)) is False   # 取引日 12/25 開始
    assert is_market_open(_dt(2025, 12, 25, 20, 59)) is False
    assert is_market_open(_dt(2025, 12, 25, 21, 0)) is True    # 取引日 12/26 開始
    assert is_market_open(_dt(2025, 12, 31, 21, 0)) is False   # 取引日 1/1
    assert is_market_open(_dt(2026, 1, 1, 21, 0)) is True      # 取引日 1/2


def test_trading_day_start_uses_fixed_21_00_utc_boundary():
    """M4: 日次開始は冬も直前の 21:00 UTC。"""
    assert trading_day_start(_dt(2026, 1, 13, 20, 59)) == _dt(2026, 1, 12, 21, 0)
    assert trading_day_start(_dt(2026, 1, 13, 21, 0)) == _dt(2026, 1, 13, 21, 0)


def test_next_rollover_uses_fixed_21_00_utc_boundary():
    """M5: 次回ロールオーバーは冬も次の 21:00 UTC。"""
    assert next_rollover(_dt(2026, 1, 13, 20, 59)) == _dt(2026, 1, 13, 21, 0)
    assert next_rollover(_dt(2026, 1, 13, 21, 0)) == _dt(2026, 1, 14, 21, 0)


def test_friday_cutoff_dst_invariant():
    """friday_swing_cutoff_ny のデフォルト "14:00" は NY 現地時間基準なので、
    市場クローズ (NY 金 17:00) までの残り時間が季節によってずれない。"""
    # 夏 (EDT, UTC-4): 18:30 UTC = NY 14:30 → cutoff 14:00 以降なので True
    assert is_friday_after(_dt(2026, 7, 24, 18, 30), "14:00") is True
    # 冬 (EST, UTC-5): 同じ 18:30 UTC でも NY 13:30 → cutoff 14:00 未満なので False
    #   (UTC の時刻だけで判定する旧実装なら、ここで誤って True になってしまっていた)
    assert is_friday_after(_dt(2026, 1, 16, 18, 30), "14:00") is False
    # 冬に NY 14:00 ちょうどになる UTC 時刻 (19:00 UTC) では True
    assert is_friday_after(_dt(2026, 1, 16, 19, 0), "14:00") is True


def test_friday_cutoff_utc_ny_weekday_mismatch():
    """UTC の曜日と NY の曜日が食い違うケース: 土曜 00:30 UTC (夏時間) は
    NY では金曜 20:30。市場は既にクローズしているので実害はないが、
    is_friday_after は NY 現地の曜日で判定するため True を返す
    (UTC の曜日だけで判定していた旧実装ではここが False になってしまっていた)。"""
    sat_utc = _dt(2026, 7, 25, 0, 30)
    assert sat_utc.weekday() == 5  # UTC では土曜
    assert is_friday_after(sat_utc, "20:00") is True  # NY では金曜 20:30


def test_market_boundary_does_not_follow_fall_back():
    """夏時間終了後も境界を22:00へ動かさず21:00 UTCを維持する。"""
    fri_close = _dt(2026, 10, 30, 21, 0)   # 金 17:00 EDT → close
    fri_open = _dt(2026, 10, 30, 20, 59)   # 金 16:59 EDT → まだ open
    sun_open = _dt(2026, 11, 1, 21, 0)
    sun_closed = _dt(2026, 11, 1, 20, 59)
    assert is_market_open(fri_open) is True
    assert is_market_open(fri_close) is False
    assert is_market_open(sun_closed) is False
    assert is_market_open(sun_open) is True


def test_naive_datetime_rejected():
    """naive datetime (tzinfo なし) は ValueError を送出する"""
    naive = datetime(2026, 7, 24, 12, 0)  # tzinfo=None

    with pytest.raises(ValueError, match="timezone-aware"):
        is_market_open(naive)

    with pytest.raises(ValueError, match="timezone-aware"):
        trading_day_start(naive)

    with pytest.raises(ValueError, match="timezone-aware"):
        next_rollover(naive)

    with pytest.raises(ValueError, match="timezone-aware"):
        is_friday_after(naive, "18:00")
