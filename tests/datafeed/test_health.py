from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.contracts import Bar, Quote
from agentic_fx.datafeed.health import (
    DataUnhealthy, validate_bars, validate_quote,
)

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _bars(n=30, start=None, step_min=60, base=148.0):
    start = start or (NOW - timedelta(minutes=step_min * n))
    return [Bar("USDJPY", "1h", start + timedelta(minutes=step_min * i),
                base, base + 0.1, base - 0.1, base + 0.05, 100)
            for i in range(n)]


def test_fresh_quote_ok():
    validate_quote(Quote("USDJPY", 148.49, 148.51, NOW, "yfinance"),
                   NOW, freshness_max_min=20)


def test_stale_quote_rejected():
    q = Quote("USDJPY", 148.49, 148.51, NOW - timedelta(minutes=25), "yfinance")
    with pytest.raises(DataUnhealthy, match="stale"):
        validate_quote(q, NOW, freshness_max_min=20)


def test_inverted_quote_rejected():
    q = Quote("USDJPY", 148.52, 148.51, NOW, "yfinance")
    with pytest.raises(DataUnhealthy, match="bid/ask"):
        validate_quote(q, NOW, freshness_max_min=20)


def test_bars_ok():
    validate_bars(_bars(), NOW, freshness_max_min=90, interval_min=60)


def test_empty_bars_rejected():
    with pytest.raises(DataUnhealthy, match="empty"):
        validate_bars([], NOW, freshness_max_min=90, interval_min=60)


def test_stale_last_bar_rejected():
    old = _bars(n=10, start=NOW - timedelta(hours=20))
    with pytest.raises(DataUnhealthy, match="stale"):
        validate_bars(old, NOW, freshness_max_min=90, interval_min=60)


def test_gap_rejected():
    bars = _bars()
    del bars[10:14]  # 4 本欠損 (市場オープン中)
    with pytest.raises(DataUnhealthy, match="gap"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)


def test_weekend_gap_is_not_a_gap():
    # 金 20:00 のバー → 日 21:00 再開のバー: 休場ギャップは欠損ではない
    fri = datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)
    sun = datetime(2026, 7, 26, 21, 0, tzinfo=timezone.utc)
    bars = [Bar("USDJPY", "1h", fri + timedelta(hours=i),
                148.0, 148.1, 147.9, 148.05, 100) for i in range(3)]
    bars += [Bar("USDJPY", "1h", sun + timedelta(hours=i),
                 148.0, 148.1, 147.9, 148.05, 100) for i in range(3)]
    validate_bars(bars, sun + timedelta(hours=3), freshness_max_min=90,
                  interval_min=60)  # 例外なし


def test_spike_rejected():
    bars = _bars()
    bad = bars[15]
    bars[15] = Bar(bad.symbol, bad.interval, bad.ts, bad.open,
                   bad.high, bad.low, bad.close * 1.2, bad.volume)  # +20%
    with pytest.raises(DataUnhealthy, match="anomal"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)


def test_zero_price_rejected():
    bars = _bars()
    bad = bars[5]
    bars[5] = Bar(bad.symbol, bad.interval, bad.ts, 0.0, bad.high,
                  bad.low, bad.close, bad.volume)
    with pytest.raises(DataUnhealthy, match="anomal"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)


# --- 修正ラウンド 1: 指摘 1 (Critical) 再現 ---
# 週末休場を跨ぐ区間全体を免除すると、休場後の実開場中の長期欠損 (別のバグ)
# を見逃してしまう。金曜最後のバー (開場中) の直後から、月曜 (休場明けから
# さらに丸1日経過した時点) までバーが一本もない = 休場分を差し引いても
# 24 本超の開場中欠損があるはずで、これは検出されなければならない。
def test_gap_spanning_weekend_with_real_missing_data_is_detected():
    fri_last = datetime(2026, 7, 24, 20, 0, tzinfo=timezone.utc)  # 金 20:00 (開場中)
    mon_bar = datetime(2026, 7, 27, 21, 0, tzinfo=timezone.utc)   # 月 21:00 (休場明けから24h後)
    bars = [
        Bar("USDJPY", "1h", fri_last, 148.0, 148.1, 147.9, 148.05, 100),
        Bar("USDJPY", "1h", mon_bar, 148.0, 148.1, 147.9, 148.05, 100),
    ]
    with pytest.raises(DataUnhealthy, match="gap"):
        validate_bars(bars, mon_bar, freshness_max_min=90, interval_min=60)


def test_uncountable_gap_fails_closed():
    # サンプル数が上限を超える場合は休場時間を計算しきれない → fail closed。
    bars = [
        Bar("USDJPY", "1m", NOW - timedelta(days=100), 148.0, 148.1, 147.9,
            148.05, 100),
        Bar("USDJPY", "1m", NOW, 148.0, 148.1, 147.9, 148.05, 100),
    ]
    with pytest.raises(DataUnhealthy, match="gap"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=1)


def test_weekend_gap_sub_hour_interval_uses_interval_granularity():
    # サンプリング刻みが interval_min に追従していることの判別テスト。
    # 15分足でぴったり休場境界に接する区間: interval_min 刻み (15分) なら
    # 休場中サンプルは 192/194、open_gap_units ≈ 1.99 (<=3, 通る) だが、
    # 固定 1 時間刻みのままだと 48/49、open_gap_units ≈ 3.9 (>3, 誤検出) になる。
    fri = datetime(2026, 7, 24, 20, 45, tzinfo=timezone.utc)  # 金 16:45 NY (開場中)
    sun = datetime(2026, 7, 26, 21, 0, tzinfo=timezone.utc)   # 日 17:00 NY (再開)
    bars = [
        Bar("USDJPY", "15m", fri, 148.0, 148.1, 147.9, 148.05, 100),
        Bar("USDJPY", "15m", sun, 148.0, 148.1, 147.9, 148.05, 100),
    ]
    validate_bars(bars, sun, freshness_max_min=90, interval_min=15)  # 例外なし


# --- 修正ラウンド 1: 指摘 2 (Important) 再現 ---
# naive datetime はプロジェクト規約 (timeutil.as_utc) に反してサイレント
# 受理されてはならない。ValueError を要求する。
def test_validate_quote_rejects_naive_now():
    q = Quote("USDJPY", 148.49, 148.51, NOW, "yfinance")
    naive_now = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError):
        validate_quote(q, naive_now, freshness_max_min=20)


def test_validate_quote_rejects_naive_quote_ts():
    naive_ts = NOW.replace(tzinfo=None)
    q = Quote("USDJPY", 148.49, 148.51, naive_ts, "yfinance")
    with pytest.raises(ValueError):
        validate_quote(q, NOW, freshness_max_min=20)


def test_validate_bars_rejects_naive_now():
    with pytest.raises(ValueError):
        validate_bars(_bars(), NOW.replace(tzinfo=None),
                      freshness_max_min=90, interval_min=60)


def test_validate_bars_rejects_naive_bar_ts():
    bars = _bars()
    bad = bars[5]
    bars[5] = Bar(bad.symbol, bad.interval, bad.ts.replace(tzinfo=None),
                  bad.open, bad.high, bad.low, bad.close, bad.volume)
    with pytest.raises(ValueError):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)


# --- 修正ラウンド 2: 祝日隣接週の回帰 再現・修正 ---
# market_hours は祝日カレンダーを持たず、12/25 のようなクリスマス (平日)
# も無条件で「開場中」と判定する。修正ラウンド 1 でバー列全体を走査する
# ようにしたことで、祝日を挟んだ「開場中の欠損」が誤検出され、しかも
# その穴がバー列の窓に残っている間 (フィードが正常に回復した後も) ずっと
# 健全性判定が落ち続けていた。gap 検査の対象を末尾直近の窓
# (_GAP_CHECK_WINDOW_BARS) に限定することで、以後のデータで回復していれば
# 通るようにする。


def test_holiday_gap_with_recovery_data_is_not_flagged():
    # 12/25 (金) はクリスマスで実質休場だが、market_hours は祝日を知らず
    # 平日として扱うため「開場中の欠損」として検出されてしまう。休場明け
    # (12/27 日 21:00) から 30h 分 (窓の 24h を超える) の連続データが
    # 積み上がった時点では、この穴は gap 検査の窓の外に出ているはずで、
    # フィードは健全と判定されなければならない。
    last_before = datetime(2026, 12, 24, 21, 0, tzinfo=timezone.utc)  # 木 (通常営業日扱い)
    reopen = datetime(2026, 12, 27, 21, 0, tzinfo=timezone.utc)       # 日 (再開)
    bars = [
        Bar("USDJPY", "1h", last_before, 148.0, 148.1, 147.9, 148.05, 100),
        Bar("USDJPY", "1h", reopen, 148.0, 148.1, 147.9, 148.05, 100),
    ]
    for i in range(1, 31):  # 30h 分の回復データ (窓 24h を超えて押し出す)
        ts = reopen + timedelta(hours=i)
        bars.append(Bar("USDJPY", "1h", ts, 148.0, 148.1, 147.9, 148.05, 100))
    now = bars[-1].ts
    validate_bars(bars, now, freshness_max_min=90, interval_min=60)  # 例外なし


def _bars_with_gap_then_recovery(recovery_hours: int) -> list[Bar]:
    """平日内で 5h (>_MAX_GAP_BARS 換算) の欠損を作り、その後 recovery_hours
    時間分の連続データを積む。休場を挟まない平日区間を使うことで、
    休場分の差し引きロジックの影響を受けずに窓の境界だけを検証できる。"""
    before_gap = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)  # 月 (平日)
    after_gap = before_gap + timedelta(hours=5)  # 5h 欠損 (gap_units=5 > 3)
    bars = [
        Bar("USDJPY", "1h", before_gap, 148.0, 148.1, 147.9, 148.05, 100),
        Bar("USDJPY", "1h", after_gap, 148.0, 148.1, 147.9, 148.05, 100),
    ]
    for i in range(1, recovery_hours + 1):
        ts = after_gap + timedelta(hours=i)
        bars.append(Bar("USDJPY", "1h", ts, 148.0, 148.1, 147.9, 148.05, 100))
    return bars


def test_gap_inside_window_is_flagged():
    # 回復データが 20h 分 (< 窓 24h) しかない → 穴はまだ窓の内側 → 検出される。
    bars = _bars_with_gap_then_recovery(recovery_hours=20)
    now = bars[-1].ts
    with pytest.raises(DataUnhealthy, match="gap"):
        validate_bars(bars, now, freshness_max_min=90, interval_min=60)


def test_gap_outside_window_is_not_flagged():
    # 回復データが 30h 分 (> 窓 24h) 積み上がった → 穴は窓の外 → 通る。
    bars = _bars_with_gap_then_recovery(recovery_hours=30)
    now = bars[-1].ts
    validate_bars(bars, now, freshness_max_min=90, interval_min=60)  # 例外なし


# --- Task 2 修正ラウンド 1: 指摘 3 (Critical) ---
# MT5 bridge はサーバのローカル時刻を無条件に UTC として解釈しているため、
# ブローカーのサーバ時刻が UTC より進んでいると未来時刻の timestamp が
# 混入し得る。既存の stale (負方向) 検査だけでは正方向のずれを検出できない。
# 注意: この health.py 内の「修正ラウンド 1/2」ラベルは祝日ギャップ回帰
# (Task 3 側レビュー) のものであり、本節の「Task 2 修正ラウンド 1」とは
# 別の修正イベント。


def test_future_quote_rejected():
    q = Quote("USDJPY", 148.49, 148.51, NOW + timedelta(minutes=10), "yfinance")
    with pytest.raises(DataUnhealthy, match="future"):
        validate_quote(q, NOW, freshness_max_min=20)


def test_future_quote_within_tolerance_ok():
    # ホスト時計のわずかなずれ (数秒〜1分程度) は許容し実運用を止めない。
    q = Quote("USDJPY", 148.49, 148.51, NOW + timedelta(seconds=30), "yfinance")
    validate_quote(q, NOW, freshness_max_min=20)  # 例外なし


def test_future_last_bar_rejected():
    bars = _bars()
    last = bars[-1]
    bars[-1] = Bar(last.symbol, last.interval, NOW + timedelta(minutes=10),
                   last.open, last.high, last.low, last.close, last.volume)
    with pytest.raises(DataUnhealthy, match="future"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)


def test_future_middle_bar_rejected():
    # 末尾だけでなく全バーを検査すること (ソースが途中に未来時刻を混ぜる
    # ケースを取りこぼさない)。
    bars = _bars()
    mid = len(bars) // 2
    m = bars[mid]
    bars[mid] = Bar(m.symbol, m.interval, NOW + timedelta(minutes=10),
                    m.open, m.high, m.low, m.close, m.volume)
    with pytest.raises(DataUnhealthy, match="future"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)


def test_bar_within_future_tolerance_ok():
    bars = _bars()
    last = bars[-1]
    bars[-1] = Bar(last.symbol, last.interval, NOW + timedelta(seconds=30),
                   last.open, last.high, last.low, last.close, last.volume)
    validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)  # 例外なし


def test_fine_grained_interval_window_has_wall_clock_floor():
    # 窓をバー本数だけで決めると 1m 足では 24 本 = 24 分しかなくなり、
    # フィード停止から 25 分程度で回復しただけで「もう健全」と誤判定して
    # しまう (advisor 指摘)。窓には wall-clock 時間の下限を設けているため、
    # 60 分の欠損 (feed 障害) の直後に 25 分だけ回復したケースは、まだ
    # 窓 (24h 下限) の内側にあり検出され続けなければならない。
    before_gap = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)  # 月 (平日)
    after_gap = before_gap + timedelta(minutes=60)  # 60分欠損 (gap_units=60 > 3)
    bars = [
        Bar("USDJPY", "1m", before_gap, 148.0, 148.1, 147.9, 148.05, 100),
        Bar("USDJPY", "1m", after_gap, 148.0, 148.1, 147.9, 148.05, 100),
    ]
    for i in range(1, 26):  # 25分だけ回復 (バー本数だけの窓なら既に外れてしまう)
        ts = after_gap + timedelta(minutes=i)
        bars.append(Bar("USDJPY", "1m", ts, 148.0, 148.1, 147.9, 148.05, 100))
    now = bars[-1].ts
    with pytest.raises(DataUnhealthy, match="gap"):
        validate_bars(bars, now, freshness_max_min=90, interval_min=1)
