"""MT5 サーバ時刻オフセットの検出・キャッシュ・双方向変換テスト。

MT5 が返す時刻はすべて「ブローカーのサーバ時間帯におけるエポック秒」で、
bridge は従来それを無条件に UTC として解釈していた。実測 (2026-07-28) では
OANDA-Japan MT5 Live は UTC+3 で、quote も OHLCV も 3 時間先の値を返していた。

検出は `symbol_info_tick(symbol).time` とこちらの UTC 時計の差から行う。
市場が閉まっている間 tick は古いので、ガード (±12h / 30分丸め / 残差 120s) を
通ったものだけ採用し、通らなければ直前の good 値を使う。good 値が 1 つも
無ければ**例外を上げて止まる** (ずれた時刻で発注やサイジングをするより安全)。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mt5_client import (
    Mt5Client,
    ServerTimeUnknownError,
    snap_server_offset,
)

UTC = timezone.utc
OFFSET = 10800          # 実測値: UTC+3
RECALC_SEC = 300        # 再計算の最短間隔


# ── Fake MT5 モジュール ────────────────────────────────────────────

class _FakeMt5:
    """offset 検出に必要な最小限の MT5 モジュール Fake。

    tick_time にサーバ時刻空間のエポック秒を入れる (None なら tick 無し)。
    """

    TIMEFRAME_M1 = 1
    TIMEFRAME_H1 = 16385
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3

    def __init__(self, *, tick_time=None, rates=None, positions=None, deals=None):
        self.tick_time = tick_time
        self.tick_calls = 0                 # 再計算回数の観測用
        self.range_args: list[tuple] = []   # copy_rates_range へ渡った引数
        self._rates = rates if rates is not None else []
        self._positions = positions
        self._deals = deals

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info_tick(self, symbol):
        self.tick_calls += 1
        if self.tick_time is None:
            return None
        return SimpleNamespace(time=self.tick_time, bid=163.934, ask=163.940)

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


def _make_client(fake, *, cache_path=None, probe_symbol="USDJPY") -> Mt5Client:
    """MT5 に触らない Mt5Client を組み立てる (__init__ は接続しない)。"""
    c = Mt5Client(
        login=1, password="x", server="OANDA-Test",
        offset_cache_path=cache_path, probe_symbol=probe_symbol,
    )
    c._mt5 = fake
    c._connected = True
    return c


def _seed_offset(client: Mt5Client, offset: int, *, fresh: bool = True) -> None:
    """good 値をメモリキャッシュに仕込む。fresh=False なら再計算窓を開ける。"""
    client._server_offset_sec = offset
    client._offset_checked_at = time.time() if fresh else 0.0
    client._offset_source = "cached"
    client._offset_disk_loaded = True


# ── 1. 純粋なガードロジック (snap_server_offset) ────────────────────

def test_snap_accepts_fresh_tick_three_hours_ahead():
    """新鮮な tick (raw ≈ +3h) から 10800 を検出する。

    実測の raw は 10799.3s。30 分丸めで 10800 になり、残差 0.7s < 120s で通る。
    """
    assert snap_server_offset(10799.3) == OFFSET


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


# ── 2. 検出・キャッシュ・fail closed ────────────────────────────────

def test_detects_offset_from_fresh_tick():
    now = time.time()
    fake = _FakeMt5(tick_time=int(now) + OFFSET)
    c = _make_client(fake)

    assert c.get_server_offset_sec() == OFFSET
    assert c.offset_source == "live"


def test_stale_tick_falls_back_to_cached_good_value():
    """古い tick は棄却され、直前の good 値がそのまま使われる (週末)。"""
    now = time.time()
    fake = _FakeMt5(tick_time=int(now) + 48 * 3600)
    c = _make_client(fake)
    _seed_offset(c, OFFSET, fresh=False)

    assert c.get_server_offset_sec() == OFFSET
    assert c.offset_source == "cached"


def test_fail_closed_when_no_good_value_ever():
    """good 値が 1 つも無く検出も棄却されたら例外。無言で 0 にしない。"""
    now = time.time()
    fake = _FakeMt5(tick_time=int(now) + 48 * 3600)
    c = _make_client(fake)

    with pytest.raises(ServerTimeUnknownError):
        c.get_server_offset_sec()


def test_fail_closed_when_no_tick_at_all():
    """tick が取れない (市場休止直後の起動など) 場合も fail closed。"""
    c = _make_client(_FakeMt5(tick_time=None))

    with pytest.raises(ServerTimeUnknownError):
        c.get_server_offset_sec()


def test_offset_is_not_recalculated_within_300s():
    """再計算は最短 300 秒間隔。2 回目は MT5 を叩かずキャッシュを返す。"""
    now = time.time()
    fake = _FakeMt5(tick_time=int(now) + OFFSET)
    c = _make_client(fake)

    assert c.get_server_offset_sec() == OFFSET
    assert fake.tick_calls == 1
    for _ in range(5):
        assert c.get_server_offset_sec() == OFFSET
    assert fake.tick_calls == 1, "300 秒以内に再計算が走っている"

    # 窓が開けば再計算する
    c._offset_checked_at = time.time() - RECALC_SEC - 1
    assert c.get_server_offset_sec() == OFFSET
    assert fake.tick_calls == 2


def test_offset_persists_to_disk_and_reloads(tmp_path):
    """ディスク永続化: 週末をまたぐ再起動でも good 値が残る。"""
    cache = tmp_path / "server_offset.json"
    now = time.time()
    c1 = _make_client(_FakeMt5(tick_time=int(now) + OFFSET), cache_path=cache)
    assert c1.get_server_offset_sec() == OFFSET
    assert json.loads(cache.read_text())["server_offset_sec"] == OFFSET

    # 再起動相当。tick は古い (週末) が、ディスクの good 値で動く
    c2 = _make_client(_FakeMt5(tick_time=int(now) + 48 * 3600), cache_path=cache)
    assert c2.get_server_offset_sec() == OFFSET
    assert c2.offset_source == "cached"


def test_corrupt_disk_cache_does_not_produce_a_guess(tmp_path):
    """壊れたキャッシュは無視する。推測値を採用せず fail closed のまま。"""
    cache = tmp_path / "server_offset.json"
    cache.write_text("{ not json")
    now = time.time()
    c = _make_client(_FakeMt5(tick_time=int(now) + 48 * 3600), cache_path=cache)

    with pytest.raises(ServerTimeUnknownError):
        c.get_server_offset_sec()


def test_disk_cache_out_of_range_is_rejected(tmp_path):
    """ディスクの値も ±12h ガードにかける (手書き事故の防止)。"""
    cache = tmp_path / "server_offset.json"
    cache.write_text(json.dumps({"server_offset_sec": 13 * 3600}))
    now = time.time()
    c = _make_client(_FakeMt5(tick_time=int(now) + 48 * 3600), cache_path=cache)

    with pytest.raises(ServerTimeUnknownError):
        c.get_server_offset_sec()


# ── 3. 受信方向の変換 (MT5 → こちら) ────────────────────────────────

def test_get_quote_time_is_shifted_back_to_true_utc():
    now = time.time()
    fake = _FakeMt5(tick_time=int(now) + OFFSET)
    c = _make_client(fake)

    q = c.get_quote("USDJPY")
    parsed = datetime.fromisoformat(q.time)
    assert parsed.tzinfo is not None
    # tick のサーバ時刻から offset を引いた = ほぼ現在の実 UTC
    assert abs((parsed - datetime.now(UTC)).total_seconds()) < 5


def test_get_positions_time_is_shifted_back_to_true_utc():
    now = time.time()
    open_utc = datetime(2026, 7, 28, 9, 30, tzinfo=UTC)
    server_epoch = int((open_utc + timedelta(seconds=OFFSET)).timestamp())
    pos = SimpleNamespace(
        ticket=1, symbol="USDJPY", type=0, volume=0.01, price_open=163.9,
        price_current=163.95, sl=0.0, tp=0.0, profit=1.0, swap=0.0,
        magic=7, comment="", time=server_epoch,
    )
    fake = _FakeMt5(tick_time=int(now) + OFFSET, positions=[pos])
    c = _make_client(fake)

    got = c.get_positions()
    assert got[0].time == open_utc.isoformat()


def test_get_closed_deal_time_is_shifted_back_to_true_utc():
    now = time.time()
    closed_utc = datetime(2026, 7, 28, 10, 15, tzinfo=UTC)
    server_epoch = int((closed_utc + timedelta(seconds=OFFSET)).timestamp())
    deals = [
        SimpleNamespace(entry=1, volume=0.01, price=163.5, profit=-100.0,
                        swap=0.0, commission=0.0, time=server_epoch, reason=4),
    ]
    fake = _FakeMt5(tick_time=int(now) + OFFSET, deals=deals)
    c = _make_client(fake)

    deal = c.get_closed_deal(111)
    assert deal is not None
    assert deal.closed_at == closed_utc.isoformat()


def test_copy_rates_range_bar_times_are_shifted_back_to_true_utc():
    now = time.time()
    bar_utc = datetime(2026, 7, 28, 11, 0, tzinfo=UTC)
    server_epoch = int((bar_utc + timedelta(seconds=OFFSET)).timestamp())
    rates = [{"time": server_epoch, "open": 163.9, "high": 164.0,
              "low": 163.8, "close": 163.935, "tick_volume": 1234}]
    fake = _FakeMt5(tick_time=int(now) + OFFSET, rates=rates)
    c = _make_client(fake)

    bars = c.copy_rates_range(
        "USDJPY", "1h",
        datetime(2026, 7, 28, 10, tzinfo=UTC),
        datetime(2026, 7, 28, 12, tzinfo=UTC),
    )
    assert bars[0]["time"] == bar_utc.isoformat()


# ── 4. 送信方向の変換 (こちら → MT5) ────────────────────────────────

def test_copy_rates_range_sends_shifted_date_range():
    """copy_rates_range は date_from / date_to もサーバ時刻空間で解釈する。

    片方向だけ直すと範囲が静かに切り詰められるので、送信側も +offset する。
    """
    now = time.time()
    fake = _FakeMt5(tick_time=int(now) + OFFSET, rates=[])
    c = _make_client(fake)

    d0 = datetime(2026, 7, 28, 0, tzinfo=UTC)
    d1 = datetime(2026, 7, 28, 12, tzinfo=UTC)
    c.copy_rates_range("USDJPY", "1h", d0, d1)

    _sym, _tf, sent_from, sent_to = fake.range_args[0]
    assert sent_from == d0 + timedelta(seconds=OFFSET)
    assert sent_to == d1 + timedelta(seconds=OFFSET)
    assert sent_from.tzinfo is not None and sent_to.tzinfo is not None


def test_copy_rates_range_rejects_naive_datetime():
    """naive datetime は解釈が曖昧なので受け取らない (tz-aware UTC 必須)。"""
    now = time.time()
    c = _make_client(_FakeMt5(tick_time=int(now) + OFFSET, rates=[]))

    with pytest.raises(ValueError, match="tz-aware"):
        c.copy_rates_range("USDJPY", "1h",
                           datetime(2026, 7, 28, 0), datetime(2026, 7, 28, 12))


def test_copy_rates_range_fails_closed_when_offset_unknown():
    """オフセット未確定なら足を返さず止まる (ずれた足でサイジングしない)。"""
    c = _make_client(_FakeMt5(tick_time=None, rates=[]))

    with pytest.raises(ServerTimeUnknownError):
        c.copy_rates_range("USDJPY", "1h",
                           datetime(2026, 7, 28, 0, tzinfo=UTC),
                           datetime(2026, 7, 28, 12, tzinfo=UTC))


# ── 5. offset = 0 の非退行 ──────────────────────────────────────────

def test_offset_zero_changes_nothing():
    """サーバが UTC のときは移植前とまったく同じ値になる (非退行)。"""
    now = time.time()
    bar_utc = datetime(2026, 7, 28, 11, 0, tzinfo=UTC)
    epoch = int(bar_utc.timestamp())
    open_utc = datetime(2026, 7, 28, 9, 30, tzinfo=UTC)
    pos = SimpleNamespace(
        ticket=1, symbol="USDJPY", type=1, volume=0.01, price_open=163.9,
        price_current=163.95, sl=0.0, tp=0.0, profit=1.0, swap=0.0,
        magic=7, comment="", time=int(open_utc.timestamp()),
    )
    deals = [SimpleNamespace(entry=1, volume=0.01, price=163.5, profit=-100.0,
                             swap=0.0, commission=0.0,
                             time=int(open_utc.timestamp()), reason=4)]
    rates = [{"time": epoch, "open": 163.9, "high": 164.0, "low": 163.8,
              "close": 163.935, "tick_volume": 1234}]
    fake = _FakeMt5(tick_time=int(now), rates=rates, positions=[pos], deals=deals)
    c = _make_client(fake)

    assert c.get_server_offset_sec() == 0

    d0 = datetime(2026, 7, 28, 10, tzinfo=UTC)
    d1 = datetime(2026, 7, 28, 12, tzinfo=UTC)
    bars = c.copy_rates_range("USDJPY", "1h", d0, d1)
    _sym, _tf, sent_from, sent_to = fake.range_args[0]
    assert (sent_from, sent_to) == (d0, d1)
    assert bars[0]["time"] == bar_utc.isoformat()
    assert c.get_positions()[0].time == open_utc.isoformat()
    assert c.get_closed_deal(1).closed_at == open_utc.isoformat()


# ── 6. lock 不変条件 ───────────────────────────────────────────────

def test_offset_resolution_happens_under_the_lock():
    """offset 解決のための MT5 呼び出しも lock の内側で行う。

    get_positions / get_closed_deal は結果の整形を lock の外でしているので、
    そこで offset を解決すると `self._mt5` を lock 外で触ることになる。
    """
    now = time.time()
    holder_seen: list[bool] = []

    class _LockCheckingMt5(_FakeMt5):
        def symbol_info_tick(self, symbol):
            # この時点で lock が保持されているか (= 他スレッドが acquire できない)
            acquired = client._lock.acquire(blocking=False)
            if acquired:
                client._lock.release()
            holder_seen.append(not acquired)
            return super().symbol_info_tick(symbol)

    fake = _LockCheckingMt5(tick_time=int(now) + OFFSET, positions=[])
    client = _make_client(fake)
    client.get_positions()

    assert holder_seen == [True], "offset 解決が lock の外で MT5 を触っている"


def test_get_server_time_reports_both_clocks():
    now = time.time()
    c = _make_client(_FakeMt5(tick_time=int(now) + OFFSET))

    info = c.get_server_time()
    assert info["server_offset_sec"] == OFFSET
    assert info["offset_source"] == "live"
    utc_dt = datetime.fromisoformat(info["utc_time"])
    srv_dt = datetime.fromisoformat(info["server_time"])
    assert (srv_dt - utc_dt).total_seconds() == OFFSET


def test_get_server_time_reads_offset_and_source_in_one_lock_section():
    """offset と offset_source は同じ lock 区間で読む (対で矛盾させない)。

    lock を離してから source を読むと、その隙に別スレッドの /quote が再検出して
    source を書き換え、返した offset とは別の解決結果の source が混ざる。
    /server-time は実データ検証の窓口なので、対がずれると調査を誤らせる。
    """
    now = time.time()
    c = _make_client(_FakeMt5(tick_time=int(now) + OFFSET))

    class _FlipOnExitLock:
        """lock を離した瞬間に別スレッドが書き換えた状況を再現する擬似 lock。"""

        def __init__(self, inner) -> None:
            self._inner = inner

        def __enter__(self):
            self._inner.acquire()
            return self

        def __exit__(self, *exc):
            self._inner.release()
            c._offset_source = "cached"
            return False

    c._lock = _FlipOnExitLock(threading.Lock())

    info = c.get_server_time()
    assert info["offset_source"] == "live", (
        "offset_source が lock の外で読まれている"
    )
