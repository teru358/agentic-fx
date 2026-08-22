"""scheduler.latest_scheduled_occurrence / period_key_of の transition matrix
(設計書 §3.1 第1文、プラン §8.1-17)。1 セル 1 テスト。"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agentic_fx.core.scheduler import latest_scheduled_occurrence, period_key_of

_UTC = ZoneInfo("UTC")


def test_weekly_now_after_occurrence_same_week():
    """weekly, at='Sat 03:00' UTC。now が土曜 03:00 より後なら同じ週の
    occurrence を返す (跨いでいない基本形)。"""
    now = datetime(2026, 8, 22, 10, 0, tzinfo=_UTC)  # 土曜
    occ = latest_scheduled_occurrence(now, cadence="weekly", at="Sat 03:00",
                                       display_timezone="UTC")
    assert occ == datetime(2026, 8, 22, 3, 0, tzinfo=_UTC)
    assert period_key_of(occ, cadence="weekly") == "2026-W34"


def test_weekly_now_before_occurrence_falls_back_to_prior_week():
    """now が土曜 03:00 より前 (同じ土曜の午前2時等) なら前週の occurrence。"""
    now = datetime(2026, 8, 22, 2, 0, tzinfo=_UTC)
    occ = latest_scheduled_occurrence(now, cadence="weekly", at="Sat 03:00",
                                       display_timezone="UTC")
    assert occ == datetime(2026, 8, 15, 3, 0, tzinfo=_UTC)
    assert period_key_of(occ, cadence="weekly") == "2026-W33"


def test_weekly_now_exactly_on_occurrence_is_inclusive():
    """`now` 以下 (以上ではない) — ちょうど occurrence 時刻なら its own
    occurrence を返す (裁定: 境界は inclusive)。"""
    now = datetime(2026, 8, 22, 3, 0, tzinfo=_UTC)
    occ = latest_scheduled_occurrence(now, cadence="weekly", at="Sat 03:00",
                                       display_timezone="UTC")
    assert occ == now


def test_daily_now_after_occurrence_same_day():
    now = datetime(2026, 8, 20, 10, 0, tzinfo=_UTC)
    occ = latest_scheduled_occurrence(now, cadence="daily", at="03:00",
                                       display_timezone="UTC")
    assert occ == datetime(2026, 8, 20, 3, 0, tzinfo=_UTC)
    assert period_key_of(occ, cadence="daily") == "2026-08-20"


def test_daily_now_before_occurrence_falls_back_to_prior_day():
    now = datetime(2026, 8, 20, 1, 0, tzinfo=_UTC)
    occ = latest_scheduled_occurrence(now, cadence="daily", at="03:00",
                                       display_timezone="UTC")
    assert occ == datetime(2026, 8, 19, 3, 0, tzinfo=_UTC)


def test_restart_after_long_downtime_still_returns_only_latest_occurrence():
    """restart: 停止が数週間に及んでも、返るのは直近 1 個の occurrence
    (catch-up は「直近の逃した occurrence のみ」— それ以前は追わない)。"""
    now = datetime(2026, 9, 30, 10, 0, tzinfo=_UTC)  # 6 週間分の occurrence を飛ばしている
    occ = latest_scheduled_occurrence(now, cadence="weekly", at="Sat 03:00",
                                       display_timezone="UTC")
    assert occ == datetime(2026, 9, 26, 3, 0, tzinfo=_UTC)
    assert period_key_of(occ, cadence="weekly") == "2026-W39"


def test_cross_period_boundary_at_exact_midnight():
    """跨 period: daily の period 境界 (現地 00:00) をまたぐ now。"""
    now = datetime(2026, 8, 21, 0, 0, 0, tzinfo=_UTC)  # 金曜 00:00、直前 occurrence は木曜 03:00
    occ = latest_scheduled_occurrence(now, cadence="daily", at="03:00",
                                       display_timezone="UTC")
    assert occ == datetime(2026, 8, 20, 3, 0, tzinfo=_UTC)
    assert period_key_of(occ, cadence="daily") == "2026-08-20"


def test_dst_spring_forward_display_timezone():
    """DST: display_timezone が夏時間へ切り替わる週でも 'Sat 03:00' は
    現地表示時刻として解釈される (UTC オフセットの変化を吸収する)。
    America/New_York は 2026-03-08 02:00 に春が進む (DST開始)。"""
    tz = "America/New_York"
    # 2026-03-14 (土) の occurrence。DST 開始 (3/8) の後なので EDT (UTC-4)。
    now = datetime(2026, 3, 14, 12, 0, tzinfo=ZoneInfo(tz))
    occ = latest_scheduled_occurrence(now, cadence="weekly", at="Sat 03:00",
                                       display_timezone=tz)
    assert occ.astimezone(ZoneInfo(tz)).hour == 3
    assert occ.astimezone(ZoneInfo(tz)).minute == 0
    assert occ.tzinfo is not None


def test_dst_fall_back_ambiguous_local_time_resolves_deterministically():
    """DST: 秋の巻き戻し週でも 1 つの occurrence だけを返す (曖昧な現地時刻
    でも `zoneinfo` の既定解決 (fold=0) で決定論的)。"""
    tz = "America/New_York"
    now = datetime(2026, 11, 8, 12, 0, tzinfo=ZoneInfo(tz))  # DST 終了週の日曜
    occ = latest_scheduled_occurrence(now, cadence="weekly", at="Sat 03:00",
                                       display_timezone=tz)
    assert occ.astimezone(ZoneInfo(tz)).hour == 3


def test_manual_overlap_does_not_change_scheduled_occurrence():
    """manual overlap: 手動 `improve` 実行の有無は `latest_scheduled_occurrence`
    の計算に一切関与しない (この関数は wave/slot の状態を読まない純関数)。
    同じ now を 2 回渡しても同じ occurrence — 呼び出し回数に非依存。"""
    now = datetime(2026, 8, 22, 10, 0, tzinfo=_UTC)
    occ1 = latest_scheduled_occurrence(now, cadence="weekly", at="Sat 03:00",
                                        display_timezone="UTC")
    occ2 = latest_scheduled_occurrence(now, cadence="weekly", at="Sat 03:00",
                                        display_timezone="UTC")
    assert occ1 == occ2


def test_m_zero_period_key_still_computed_but_caller_writes_nothing():
    """M=0 (space 全部 busy) は ImproveSupervisor.tick の責務 — この関数
    自体は M と無関係に period key を返す (関心の分離 pin)。"""
    now = datetime(2026, 8, 22, 10, 0, tzinfo=_UTC)
    occ = latest_scheduled_occurrence(now, cadence="weekly", at="Sat 03:00",
                                       display_timezone="UTC")
    assert period_key_of(occ, cadence="weekly") is not None


@pytest.mark.parametrize("at", ["25:00", "Sat 3:00", "Funday 03:00", "03:60"])
def test_invalid_at_format_raises(at):
    with pytest.raises(ValueError, match="invalid 'at' format"):
        latest_scheduled_occurrence(
            datetime(2026, 8, 22, 10, 0, tzinfo=_UTC),
            cadence="weekly" if " " in at else "daily", at=at,
            display_timezone="UTC")
