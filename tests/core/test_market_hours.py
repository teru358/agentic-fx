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


# --- レビュー修正 (codex 6): NY 現地時間 17:00 基準 (冬時間は 22:00 UTC) ---

def test_winter_dst_boundary_is_22_00_utc():
    """NY 標準時 (EST, UTC-5) の 17:00 は 22:00 UTC (夏時間なら 21:00 UTC)。
    固定 21:00 UTC のままだと冬季に日次リセット・day 強制決済が
    本来より 1 時間早まってしまう (資金制限の意味が変わる — Critical 級)。"""
    fri_open = _dt(2026, 1, 16, 21, 59)   # 金 21:59 UTC = 16:59 EST (冬) → まだ open
    fri_close = _dt(2026, 1, 16, 22, 0)   # 金 22:00 UTC = 17:00 EST (冬) → close
    assert is_market_open(fri_open) is True
    assert is_market_open(fri_close) is False

    assert trading_day_start(_dt(2026, 1, 14, 12, 0)) == _dt(2026, 1, 13, 22, 0)
    assert trading_day_start(_dt(2026, 1, 14, 23, 0)) == _dt(2026, 1, 14, 22, 0)

    assert next_rollover(_dt(2026, 1, 14, 12, 0)) == _dt(2026, 1, 14, 22, 0)


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
