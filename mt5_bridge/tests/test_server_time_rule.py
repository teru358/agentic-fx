"""サーバ時刻の換算 (NY の規則) と、測定ガード (snap_server_offset) のテスト。

MT5 が返す時刻はすべて「ブローカーのサーバ時間帯のエポック秒」で、サーバ時刻は
NY の壁時計 + 7 時間 (夏 UTC+3 / 冬 UTC+2)。bridge は返った時刻を呼び出しごとに規則で
真の UTC へ戻し、要求の範囲は規則の逆でサーバ時刻へ送る。tick から測った offset は
換算に使わず、規則との一致の検査にだけ使う (状態の test は test_time_rule_state.py)。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mt5_client import Mt5Client, snap_server_offset, utc_to_server_epoch

UTC = timezone.utc
SUMMER = 10800      # 2026-07-28 の規則
WINTER = 7200       # 2026-11-02 の規則


def _at(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class _FakeMt5:
    """換算に必要な最小限の MT5 モジュール Fake。tick は呼び出しの時計に追従する。"""

    TIMEFRAME_M1 = 1
    TIMEFRAME_H1 = 16385
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3

    def __init__(self, now, offset, *, rates=None, positions=None, deals=None):
        self.now = now
        self.offset = offset
        self.range_args: list[tuple] = []
        self._rates = rates if rates is not None else []
        self._positions = positions
        self._deals = deals

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(time=int(self.now.timestamp()) + self.offset,
                               bid=163.934, ask=163.940)

    def symbol_info(self, symbol):
        return SimpleNamespace(spread=12)

    def copy_rates_range(self, symbol, tf, date_from, date_to):
        self.range_args.append((symbol, tf, date_from, date_to))
        return self._rates

    def positions_get(self, **kwargs):
        return self._positions

    def history_deals_get(self, position):
        return self._deals

    def last_error(self):
        return (0, "ok")


def _make_client(now, offset, **kw) -> Mt5Client:
    """MT5 に触らない Mt5Client を組み立てる。測定は規則と一致する tick を返す。"""
    c = Mt5Client(login=1, password="x", server="OANDA-Test")
    c._mt5 = _FakeMt5(now, offset, **kw)
    c._connected = True
    c._now = lambda: now
    return c


def _server_epoch(utc: datetime) -> int:
    return utc_to_server_epoch(utc)


# ── 1. 純粋なガードロジック (snap_server_offset) ────────────────────

def test_snap_accepts_fresh_tick_three_hours_ahead():
    """新鮮な tick (raw ≈ +3h) から 10800 を検出する。

    実測の raw は 10799.3s。30 分丸めで 10800 になり、残差 0.7s < 120s で通る。
    """
    assert snap_server_offset(10799.3) == SUMMER


def test_snap_rejects_stale_tick_beyond_twelve_hours():
    """古い tick (raw ≈ +48h) は ±12h ガードで棄却される (週末の欠測)。"""
    assert snap_server_offset(48 * 3600 + 12.0) is None


def test_snap_rejects_large_rounding_residual():
    """raw = 3h + 5 分は丸め残差 300s > 120s なので棄却 (tick が古い証拠)。"""
    assert snap_server_offset(3 * 3600 + 300) is None


def test_snap_accepts_zero_offset_when_server_is_utc():
    """サーバが UTC のときは 0 を返す (None ではない)。"""
    assert snap_server_offset(-1.2) == 0


def test_snap_accepts_negative_and_half_hour_offsets():
    """負のオフセットと 30 分単位のオフセットも採用対象。"""
    assert snap_server_offset(-7200.4) == -7200
    assert snap_server_offset(5400.5) == 5400


# ── 2. 受信方向の換算 (MT5 → こちら): 真の UTC になる ─────────────────

NOWS = [(_at(2026, 7, 28, 12), SUMMER), (_at(2026, 11, 2, 12), WINTER)]


@pytest.mark.parametrize(("now", "offset"), NOWS)
def test_get_quote_time_is_true_utc(now, offset):
    c = _make_client(now, offset)

    q = c.get_quote("USDJPY")

    assert datetime.fromisoformat(q.time) == now


@pytest.mark.parametrize(("opened", "now", "offset"), [
    (_at(2026, 7, 28, 9, 30), _at(2026, 7, 28, 12), SUMMER),
    (_at(2026, 7, 28, 9, 30), _at(2026, 11, 2, 12), WINTER),    # 夏の建玉を冬に読む
    (_at(2026, 11, 2, 9, 30), _at(2026, 7, 28, 12), SUMMER),    # 冬の建玉を夏に読む
])
def test_get_positions_time_is_true_utc(opened, now, offset):
    pos = SimpleNamespace(
        ticket=1, symbol="USDJPY", type=0, volume=0.01, price_open=163.9,
        price_current=163.95, sl=0.0, tp=0.0, profit=1.0, swap=0.0,
        magic=7, comment="", time=_server_epoch(opened),
    )
    c = _make_client(now, offset, positions=[pos])

    assert c.get_positions()[0].time == opened.isoformat()


@pytest.mark.parametrize(("closed", "now", "offset"), [
    (_at(2026, 7, 28, 10, 15), _at(2026, 7, 28, 12), SUMMER),
    (_at(2026, 7, 28, 10, 15), _at(2026, 11, 2, 12), WINTER),
    (_at(2026, 11, 2, 10, 15), _at(2026, 7, 28, 12), SUMMER),
])
def test_get_closed_deal_time_is_true_utc(closed, now, offset):
    deals = [SimpleNamespace(entry=1, volume=0.01, price=163.5, profit=-100.0,
                             swap=0.0, commission=0.0, time=_server_epoch(closed),
                             reason=4)]
    c = _make_client(now, offset, deals=deals)

    deal = c.get_closed_deal(111)

    assert deal is not None
    assert deal.closed_at == closed.isoformat()


@pytest.mark.parametrize(("now", "offset"), NOWS)
def test_copy_rates_range_labels_are_true_utc_for_summer_and_winter_bars(now, offset):
    bars_utc = [_at(2026, 10, 30, 20, 59), _at(2026, 11, 2, 22, 0)]
    rates = [{"time": _server_epoch(b), "open": 1.0, "high": 2.0, "low": 0.5,
              "close": 1.5, "tick_volume": 7} for b in bars_utc]
    c = _make_client(now, offset, rates=rates)

    bars = c.copy_rates_range("USDJPY", "1m", _at(2026, 10, 30), _at(2026, 11, 3))

    assert [b["time"] for b in bars] == [b.isoformat() for b in bars_utc]
    assert bars[0]["volume"] == 7.0


# ── 3. 送信方向の換算 (こちら → MT5): 規則の逆 ─────────────────────────

@pytest.mark.parametrize(("date_from", "date_to", "sent_from", "sent_to"), [
    # 夏 (UTC+3)
    (_at(2026, 7, 28, 0), _at(2026, 7, 28, 12),
     _at(2026, 7, 28, 3), _at(2026, 7, 28, 15)),
    # 2026-11-01 の切替をまたぐ (夏 UTC+3 から冬 UTC+2)
    (_at(2026, 11, 1, 0), _at(2026, 11, 2, 0),
     _at(2026, 11, 1, 3), _at(2026, 11, 2, 2)),
    # 2027-03-14 の切替をまたぐ (冬 UTC+2 から夏 UTC+3)
    (_at(2027, 3, 14, 0), _at(2027, 3, 15, 0),
     _at(2027, 3, 14, 2), _at(2027, 3, 15, 3)),
])
def test_copy_rates_range_sends_the_inverse_of_the_rule(date_from, date_to, sent_from, sent_to):
    c = _make_client(_at(2026, 7, 28, 12), SUMMER)

    c.copy_rates_range("USDJPY", "1h", date_from, date_to)

    _sym, _tf, got_from, got_to = c._mt5.range_args[0]
    assert (got_from, got_to) == (sent_from, sent_to)
    assert got_from.tzinfo is not None and got_to.tzinfo is not None


def test_request_is_identical_whatever_the_clock_says_now():
    results = []
    for now, offset in NOWS:
        c = _make_client(now, offset)
        c.copy_rates_range("USDJPY", "1h", _at(2026, 10, 30), _at(2026, 11, 3))
        results.append(c._mt5.range_args[0][2:])
    assert results[0] == results[1]


def test_copy_rates_range_accepts_other_aware_zones_as_the_same_instant():
    from datetime import timedelta
    jst = timezone(timedelta(hours=9))
    c = _make_client(_at(2026, 7, 28, 12), SUMMER)

    c.copy_rates_range("USDJPY", "1h", datetime(2026, 7, 28, 9, tzinfo=jst),
                       datetime(2026, 7, 28, 21, tzinfo=jst))

    assert c._mt5.range_args[0][2:] == (_at(2026, 7, 28, 3), _at(2026, 7, 28, 15))


def test_copy_rates_range_rejects_naive_datetime():
    """naive datetime は解釈が曖昧なので受け取らない (tz-aware 必須)。"""
    c = _make_client(_at(2026, 7, 28, 12), SUMMER)

    with pytest.raises(ValueError, match="tz-aware"):
        c.copy_rates_range("USDJPY", "1h",
                           datetime(2026, 7, 28, 0), datetime(2026, 7, 28, 12))
    assert c._mt5.range_args == []


# ── 4. lock 不変条件 ───────────────────────────────────────────────

def test_measurement_happens_under_the_lock():
    """規則の検査のための MT5 呼び出しも lock の内側で行う。

    get_positions は結果の整形を lock の外でしているので、そこで tick を読むと
    `self._mt5` を lock 外で触ることになる。
    """
    holder_seen: list[bool] = []

    class _LockCheckingMt5(_FakeMt5):
        def symbol_info_tick(self, symbol):
            acquired = client._lock.acquire(blocking=False)
            if acquired:
                client._lock.release()
            holder_seen.append(not acquired)
            return super().symbol_info_tick(symbol)

    now = _at(2026, 7, 28, 12)
    client = Mt5Client(login=1, password="x", server="t")
    client._mt5 = _LockCheckingMt5(now, SUMMER, positions=[])
    client._now = lambda: now
    client.get_positions()
    client.get_server_time()

    assert holder_seen == [True], "測定が lock の外で MT5 を触っている"


def test_get_server_time_reports_both_clocks():
    now = _at(2026, 7, 28, 12)
    c = _make_client(now, SUMMER)

    info = c.get_server_time()

    assert info["server_offset_sec"] == SUMMER
    assert info["offset_source"] == "rule"
    assert datetime.fromisoformat(info["utc_time"]) == now
    assert (datetime.fromisoformat(info["server_time"])
            - datetime.fromisoformat(info["utc_time"])).total_seconds() == SUMMER


def test_get_server_time_reads_clocks_and_state_in_one_lock_section():
    """両方の時計と状態は同じ lock 区間で読む (対で矛盾させない)。

    lock を離してから状態を読むと、その隙に別スレッドの /quote が測定して
    状態だけ書き換わり、返した時計とは別の解決結果が混ざる。/server-time は
    実データ検証の窓口なので、対がずれると調査を誤らせる。
    """
    now = _at(2026, 7, 28, 12)
    c = _make_client(now, SUMMER)

    class _FlipOnExitLock:
        """lock を離した瞬間に別スレッドが書き換えた状況を再現する擬似 lock。"""

        def __init__(self, inner) -> None:
            self._inner = inner

        def __enter__(self):
            self._inner.acquire()
            return self

        def __exit__(self, *exc):
            self._inner.release()
            c._measured_offset_sec = 7200
            c._mismatch_latched = True
            return False

    c._lock = _FlipOnExitLock(threading.Lock())

    info = c.get_server_time()

    assert info["measured_offset_sec"] == SUMMER
    assert info["offset_matches_rule"] is True
    assert info["time_rule_state"] == "ok"
